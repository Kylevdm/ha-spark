"""V2L (Vehicle-to-Load) observe + tally + notify.

ha-spark reads the car's V2L discharge-power sensor (W), integrates it into the
energy delivered this session, values it against the configured tariff (less
conversion losses), publishes sensor.ha_spark_v2l_*, and fires timely HA
notifications. V2L is a manual physical adapter with no control API: this is
read/observe + notify only. The planner and chargers are untouched.

It also asks the owner for a V2L top-up when an Axle export event is
underfunded and the charge window can't fix it (#208): the overnight charge
was capped by the current ceiling, or on the event day the SoC is well below
what the overnight plan expected. The request is a notification only.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ChargePlan, FlexibilityEvent
from ha_spark.energy.sources import _to_float, parse_time
from ha_spark.ha.rest import HomeAssistantRest, notify
from ha_spark.logging import get_logger

log = get_logger(__name__)

# ponytail: rectangle integration + dt clamp; upgrade to trapezoid only if the
# 60 s tick proves too coarse (it won't for kWh-scale tallies).
_IDLE_W = 50.0  # power below this = V2L idle/stopped
_DT_CLAMP_S = 300.0  # integration gap ceiling (restart-safe)
_PLUG_IN_LEAD_MIN = 20.0  # N3 predictive lead time
_CUTOFF_WINDOW_MIN = 120.0  # N1 fires only within this many minutes after cutoff
_RESEND_GROWTH_KWH = 0.5  # a top-up request repeats once the shortfall grows this much

Entity = tuple[str, str, dict[str, Any]]


@dataclass
class V2LSession:
    """The running tally for one V2L session, persisted across restarts."""

    day: str  # local ISO date the session belongs to (drives the daily reset)
    kwh_delivered: float = 0.0
    last_power_w: float = 0.0
    peak_power_w: float = 0.0
    last_sample_ts: str | None = None  # ISO of the last sample; None until first
    active: bool = False
    notified_unplug: bool = False
    notified_plug_in: bool = False
    notified_budget: bool = False


def integrate(prev_kwh: float, power_w: float, dt_s: float) -> float:
    """Add one rectangle of energy (kWh) to the running total.

    Pure rectangle rule. The caller (``apply_sample``) clamps ``dt_s`` to
    ``_DT_CLAMP_S`` first, so a long downtime gap can't inflate the tally.
    """
    return prev_kwh + (power_w / 1000.0) * (dt_s / 3600.0)


def apply_sample(session: V2LSession, power_w: float, now: datetime) -> V2LSession:
    """Fold one power reading into the session and return it (mutates in place).

    Resets to a fresh session only when the calendar day has rolled over AND
    V2L is idle, so a session running across midnight is never cut mid-discharge.
    The sample interval is clamped to ``_DT_CLAMP_S`` so a restart gap can't
    inflate the tally.
    """
    today = now.date().isoformat()
    if session.day and session.day != today and power_w < _IDLE_W:
        session = V2LSession(day=today)
    if not session.day:
        session.day = today

    dt_s = 0.0  # first sample, or a malformed/mixed-tz stored timestamp: no interval
    if session.last_sample_ts is not None:
        try:
            prev = datetime.fromisoformat(session.last_sample_ts)
        except (ValueError, TypeError):
            prev = None
        # ponytail: skip-one-interval on tz mismatch; can't recover a naive value's
        # true offset, and losing one 60 s tick of kWh is negligible.
        if prev is not None and (prev.tzinfo is None) == (now.tzinfo is None):
            dt_s = min(_DT_CLAMP_S, max(0.0, (now - prev).total_seconds()))

    session.kwh_delivered = integrate(session.kwh_delivered, power_w, dt_s)
    session.last_power_w = power_w
    session.peak_power_w = max(session.peak_power_w, power_w)
    session.active = power_w >= _IDLE_W
    session.last_sample_ts = now.isoformat()
    return session


def delivered_fraction(settings: Settings) -> float:
    """Share of the car's AC output that reaches the house.

    V2L runs through the rectifier into the house battery's DC side, so it
    loses the rectifier and the battery's discharge leg (taken as the square
    root of the round trip). It skips the inverter's charge leg.
    """
    return settings.v2l_rectifier_efficiency * math.sqrt(max(0.0, settings.charge_efficiency))


def savings(
    kwh: float, peak: float, offpeak: float, eff: float, delivered: float = 1.0
) -> tuple[float, float, float]:
    """Return ``(avoided, refill_cost, net)`` GBP for ``kwh`` delivered via V2L.

    The V2L sensor reads AC out of the car; only ``kwh * delivered`` of it
    reaches the house and offsets peak import (see ``delivered_fraction``).
    Putting ``kwh`` back into the car draws ``kwh / eff`` from the grid at the
    cheap rate. ``net`` may be negative.
    """
    avoided = kwh * delivered * peak
    refill = (kwh / eff) * offpeak if eff > 0 else 0.0
    return avoided, refill, avoided - refill


@dataclass
class Notice:
    """One pending HA notification; ``flag`` is the session attr set once fired."""

    flag: str
    title: str
    message: str


def _minutes_after(now: time, cutoff: time) -> float:
    """Minutes from ``cutoff`` to ``now`` within a day, wrapping at midnight."""
    now_m = now.hour * 60 + now.minute
    cut_m = cutoff.hour * 60 + cutoff.minute
    return float((now_m - cut_m) % (24 * 60))


def notification_service(settings: Settings) -> str:
    """Resolve the shared notification target with the deprecated V2L fallback."""
    return settings.notify_service or settings.v2l_notify_service


def warn_deprecated_notify_target(settings: Settings) -> None:
    """Warn when V2L notifications rely on the deprecated service option."""
    if not settings.notify_service and settings.v2l_notify_service:
        log.warning("v2l_notify_service is deprecated; use notify_service instead")


def notifications(session: V2LSession, now: datetime, settings: Settings) -> list[Notice]:
    """Return the fire-once notices whose trigger holds (empty if notify off)."""
    if not notification_service(settings):
        return []

    out: list[Notice] = []
    _, _, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
        delivered_fraction(settings),
    )

    # N1 - unplug at cutoff: still discharging within the post-cutoff window.
    cutoff = parse_time(settings.v2l_cutoff_time)
    if (
        not session.notified_unplug
        and session.active
        and _minutes_after(now.time(), cutoff) <= _CUTOFF_WINDOW_MIN
    ):
        out.append(
            Notice(
                "notified_unplug",
                "Unplug V2L",
                f"Cheap window starting - unplug V2L. Tonight: "
                f"{session.kwh_delivered:.1f} kWh, net GBP {net:.2f}.",
            )
        )

    # N2 - plug in to recharge: delivered something and V2L has now stopped.
    if not session.notified_plug_in and not session.active and session.kwh_delivered > 0:
        out.append(
            Notice(
                "notified_plug_in",
                "Plug in to recharge",
                f"V2L done - {session.kwh_delivered:.1f} kWh pulled. "
                f"Plug the car in to recharge on the cheap rate.",
            )
        )

    # N3 - predictive plug-in: projected to hit the V2L budget within the lead.
    if not session.notified_budget and settings.v2l_budget_kwh > 0:
        remaining = settings.v2l_budget_kwh - session.kwh_delivered
        hit = remaining <= 0
        if not hit and session.last_power_w > 0:
            mins = (remaining / (session.last_power_w / 1000.0)) * 60.0
            hit = mins <= _PLUG_IN_LEAD_MIN
        if hit:
            out.append(
                Notice(
                    "notified_budget",
                    "Car nearing V2L budget",
                    f"Car will reach your V2L budget "
                    f"({settings.v2l_budget_kwh:.0f} kWh) soon - plan to plug in.",
                )
            )
    return out


def payload(session: V2LSession, settings: Settings) -> list[Entity]:
    """Map the session to (entity_id, state, attributes) sensor tuples."""
    avoided, refill, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
        delivered_fraction(settings),
    )
    return [
        (
            "sensor.ha_spark_v2l_power_w",
            f"{session.last_power_w:.0f}",
            {
                "friendly_name": "ha-spark V2L power",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
        ),
        (
            "sensor.ha_spark_v2l_energy_kwh",
            f"{session.kwh_delivered:.2f}",
            {
                "friendly_name": "ha-spark V2L energy",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
            },
        ),
        (
            "sensor.ha_spark_v2l_net_saving_gbp",
            f"{net:.2f}",
            {
                "friendly_name": "ha-spark V2L net saving",
                "unit_of_measurement": "GBP",
                "device_class": "monetary",
                "state_class": "measurement",
                "avoided_gbp": round(avoided, 2),
                "refill_cost_gbp": round(refill, 2),
                "peak_power_w": round(session.peak_power_w, 0),
            },
        ),
    ]


def _session_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "ha_spark_v2l_session.json"


def load_session(settings: Settings) -> V2LSession:
    """Load the persisted session, or a fresh one if absent/corrupt."""
    path = _session_path(settings)
    if not path.is_file():
        return V2LSession(day="")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return V2LSession(**data)
    except (OSError, ValueError, TypeError):
        log.warning("Reading V2L session failed; starting fresh", exc_info=True)
        return V2LSession(day="")


def save_session(settings: Settings, session: V2LSession) -> None:
    """Persist the session to /data (best-effort, atomic via tmp-file + rename)."""
    path = _session_path(settings)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(asdict(session)), encoding="utf-8")
        os.replace(tmp_path, path)  # ponytail: stdlib atomic rename, no WAL needed
    except OSError:
        log.warning("Caching V2L session failed", exc_info=True)
        tmp_path.unlink(missing_ok=True)


# --- V2L top-up request for an underfunded Axle event (#208) ---


@dataclass
class TopUpRecord:
    """Per-event state for the top-up request, persisted across restarts.

    ``path`` is the expected SoC path of the last plan computed inside the
    charge window, as ``(iso, pct)`` pairs, and ``capped`` that plan's verdict
    on the charge-window ceiling. ``last_sent_kwh`` is the shortfall the last
    request reported.
    """

    event_id: str
    path: list[tuple[str, float]] = field(default_factory=list)
    capped: bool = False
    last_sent_kwh: float | None = None


@dataclass(frozen=True)
class TopUp:
    """One V2L top-up request: how much, how soon, and why."""

    shortfall_kwh: float  # battery energy missing at ``needed_at``
    car_kwh: float  # car AC energy to draw, within the V2L budget
    needed_at: datetime
    start_by: datetime
    budget_capped: bool
    reason: str


def event_id(event: FlexibilityEvent) -> str:
    """The ``direction|start|end`` identity the export store and notices use."""
    return (
        f"{event.direction}|{event.start.astimezone(UTC).isoformat()}|"
        f"{event.end.astimezone(UTC).isoformat()}"
    )


def _soc_at(path: list[tuple[datetime, float]], when: datetime) -> float | None:
    """Expected SoC at ``when``, interpolated within a slot; None off the path."""
    for (start, soc), (next_start, next_soc) in zip(path, path[1:], strict=False):
        if start <= when < next_start:
            frac = (when - start) / (next_start - start)
            return soc + (next_soc - soc) * frac
    if path and path[-1][0] <= when < path[-1][0] + timedelta(minutes=30):
        return path[-1][1]
    return None


def topup_request(
    plan: ChargePlan,
    event: FlexibilityEvent,
    record: TopUpRecord,
    now: datetime,
    settings: Settings,
) -> TopUp | None:
    """The V2L top-up to request for ``event``, or None when none is due.

    Only when a paid slot stays unfunded after the post-event-reserve trade,
    V2L energy costs less than the event pays, and the charge window can't fix
    it: it was capped by the current ceiling, or (on the event day) live SoC is
    more than ``v2l_soc_tolerance_pct`` below the overnight plan's path.
    """
    needed_pct, needed_at = plan.export_soc_needed_pct, plan.export_soc_needed_at
    if needed_pct is None or needed_at is None or not plan.soc.ok:
        return None
    delivered = delivered_fraction(settings)
    car_eff = settings.v2l_round_trip_efficiency
    if car_eff <= 0 or event.rate_gbp_kwh <= settings.v2l_offpeak_rate_gbp / (car_eff * delivered):
        return None

    maxed = "overnight charge maxed"
    offset = 0.0
    if _soc_at(list(plan.soc_path), needed_at) is not None:
        # Until the event day's window closes, this plan's own path covers it.
        if not plan.overnight_charge_capped:
            return None
        path, reason = list(plan.soc_path), maxed
    else:
        # Afterwards, the overnight plan's path, shifted by how far live SoC
        # has strayed from it.
        path = [(datetime.fromisoformat(ts), pct) for ts, pct in record.path]
        expected_now = _soc_at(path, now)
        if expected_now is None:
            return None
        offset = plan.soc_now - expected_now
        if record.capped:
            reason = maxed
        elif offset < -settings.v2l_soc_tolerance_pct:
            reason = f"SoC {plan.soc_now:.0f}% is {-offset:.0f} points below plan"
        else:
            return None
    expected = _soc_at(path, needed_at)
    if expected is None:
        return None

    shortfall_kwh = (min(needed_pct, 100.0) - (expected + offset)) / 100.0 * plan.capacity_kwh
    if shortfall_kwh <= 1e-9:
        return None
    car_kwh = shortfall_kwh / delivered
    budget_capped = 0 < settings.v2l_budget_kwh < car_kwh
    if budget_capped:
        car_kwh = settings.v2l_budget_kwh
    hours = car_kwh * settings.v2l_rectifier_efficiency / settings.v2l_charge_kw
    return TopUp(
        shortfall_kwh=shortfall_kwh,
        car_kwh=car_kwh,
        needed_at=needed_at,
        start_by=needed_at - timedelta(hours=hours),
        budget_capped=budget_capped,
        reason=reason,
    )


def topup_notice(topup: TopUp, event: FlexibilityEvent, now: datetime) -> Notice:
    """The owner-facing request: shortfall, how much from the car, start time."""
    tz = now.tzinfo
    start, end = event.start.astimezone(tz), event.end.astimezone(tz)
    when = (
        "now"
        if topup.start_by <= now
        else f"by {topup.start_by.astimezone(tz):%H:%M}"
    )
    message = (
        f"Axle export {start:%H:%M}-{end:%H:%M} is short by {topup.shortfall_kwh:.1f} kWh "
        f"({topup.reason}). Start V2L {when} to draw about {topup.car_kwh:.1f} kWh "
        f"from the car before {topup.needed_at.astimezone(tz):%H:%M}."
    )
    if topup.budget_capped:
        message += " That is your whole V2L budget, so the event stays partly short."
    message += " Without it, ha-spark skips the unfunded slots."
    return Notice("topup", "Start V2L for the Axle event", message)


def _in_charge_window(plan: ChargePlan, now: datetime) -> bool:
    """Whether ``now`` is inside the window this plan's path starts after."""
    if not plan.soc_path:
        return False
    window_end = plan.soc_path[0][0]
    return window_end - timedelta(hours=plan.window_hours) <= now < window_end


def _topup_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "ha_spark_v2l_topup.json"


def load_topup(settings: Settings, event: str) -> TopUpRecord:
    """The persisted record for ``event``, or a fresh one (other event, absent, corrupt)."""
    try:
        data = json.loads(_topup_path(settings).read_text(encoding="utf-8"))
        record = TopUpRecord(
            event_id=str(data["event_id"]),
            path=[(str(ts), float(pct)) for ts, pct in data["path"]],
            capped=bool(data["capped"]),
            last_sent_kwh=(
                None if data["last_sent_kwh"] is None else float(data["last_sent_kwh"])
            ),
        )
        for ts, _ in record.path:
            datetime.fromisoformat(ts)
    except FileNotFoundError:
        return TopUpRecord(event_id=event)
    except (OSError, ValueError, TypeError, KeyError):
        log.warning("Reading V2L top-up state failed; starting fresh", exc_info=True)
        return TopUpRecord(event_id=event)
    return record if record.event_id == event else TopUpRecord(event_id=event)


def save_topup(settings: Settings, record: TopUpRecord) -> None:
    """Persist the record (best-effort, atomic via tmp-file + rename)."""
    path = _topup_path(settings)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(asdict(record)), encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        log.warning("Caching V2L top-up state failed", exc_info=True)
        tmp_path.unlink(missing_ok=True)


async def run_v2l_topup(
    settings: Settings,
    rest: HomeAssistantRest,
    plan: ChargePlan,
    event: FlexibilityEvent | None,
    now: datetime,
) -> None:
    """Record the overnight path, then request a V2L top-up if one is due.

    Sends again only once the shortfall has grown by ``_RESEND_GROWTH_KWH``.
    Best-effort: a failed send retries next plan and never raises.
    """
    service = notification_service(settings)
    if event is None or event.direction != "export" or not service:
        return
    record = load_topup(settings, event_id(event))
    if _in_charge_window(plan, now):
        record.path = [(ts.isoformat(), pct) for ts, pct in plan.soc_path]
        record.capped = plan.overnight_charge_capped
    topup = topup_request(plan, event, record, now, settings)
    if topup is not None and (
        record.last_sent_kwh is None
        or topup.shortfall_kwh >= record.last_sent_kwh + _RESEND_GROWTH_KWH
    ):
        notice = topup_notice(topup, event, now.astimezone(load_timezone(settings.timezone)))
        try:
            await notify(rest, service, notice.title, notice.message)
            record.last_sent_kwh = topup.shortfall_kwh
        except Exception:  # noqa: BLE001 - a failed send retries next plan
            log.warning("V2L top-up notify failed", exc_info=True)
    save_topup(settings, record)


async def run_v2l_tick(settings: Settings, now: datetime) -> None:
    """One V2L pass: read, integrate, publish sensors, notify, persist.

    Best-effort and self-contained (opens its own REST client), mirroring
    ``scheduler.sample_signals``. An unreadable sensor logs and returns; it
    never raises into the daemon loop.
    """
    session = load_session(settings)
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        try:
            state = await rest.get_state(settings.v2l_power_entity)
        except Exception as exc:  # noqa: BLE001 - never break the loop on bad data
            log.warning("V2L: %s unreadable (%s); skipping", settings.v2l_power_entity, exc)
            return
        power_w = _to_float(state.state, 0.0)
        session = apply_sample(session, power_w, now)

        for entity_id, value, attrs in payload(session, settings):
            try:
                await rest.set_state(entity_id, value, attrs)
            except Exception:  # noqa: BLE001 - publishing is best-effort
                log.warning("Publishing %s failed", entity_id, exc_info=True)

        for notice in notifications(session, now, settings):
            try:
                await notify(rest, notification_service(settings), notice.title, notice.message)
                setattr(session, notice.flag, True)  # flag only on success
            except Exception:  # noqa: BLE001 - a failed send retries next tick
                log.warning("V2L notify (%s) failed", notice.flag, exc_info=True)

    save_session(settings, session)
