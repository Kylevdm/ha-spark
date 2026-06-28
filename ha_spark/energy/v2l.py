"""V2L (Vehicle-to-Load) observe + tally + notify.

ha-spark reads the car's V2L discharge-power sensor (W), integrates it into the
energy delivered this session, values it against the configured tariff (less a
round-trip efficiency), publishes sensor.ha_spark_v2l_*, and fires timely HA
notifications. V2L is a manual physical adapter with no control API: this is
read/observe + notify only. The planner and chargers are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

from ha_spark.config import Settings
from ha_spark.energy.sources import parse_time

# ponytail: rectangle integration + dt clamp; upgrade to trapezoid only if the
# 60 s tick proves too coarse (it won't for kWh-scale tallies).
_IDLE_W = 50.0  # power below this = V2L idle/stopped
_DT_CLAMP_S = 300.0  # integration gap ceiling (restart-safe)
_PLUG_IN_LEAD_MIN = 20.0  # N3 predictive lead time
_CUTOFF_WINDOW_MIN = 120.0  # N1 fires only within this many minutes after cutoff

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

    if session.last_sample_ts is not None:
        prev = datetime.fromisoformat(session.last_sample_ts)
        dt_s = min(_DT_CLAMP_S, max(0.0, (now - prev).total_seconds()))
    else:
        dt_s = 0.0  # first sample: no interval to integrate

    session.kwh_delivered = integrate(session.kwh_delivered, power_w, dt_s)
    session.last_power_w = power_w
    session.peak_power_w = max(session.peak_power_w, power_w)
    session.active = power_w >= _IDLE_W
    session.last_sample_ts = now.isoformat()
    return session


def savings(kwh: float, peak: float, offpeak: float, eff: float) -> tuple[float, float, float]:
    """Return ``(avoided, refill_cost, net)`` GBP for ``kwh`` delivered via V2L.

    The V2L sensor reads AC out of the car, so ``kwh`` offsets peak import
    directly. The losses bite on the refill: putting ``kwh`` back into the car
    draws ``kwh / eff`` from the grid at the cheap rate. ``net`` may be negative.
    """
    avoided = kwh * peak
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


def notifications(session: V2LSession, now: datetime, settings: Settings) -> list[Notice]:
    """Return the fire-once notices whose trigger holds (empty if notify off)."""
    if not settings.v2l_notify_service:
        return []

    out: list[Notice] = []
    _, _, net = savings(
        session.kwh_delivered,
        settings.v2l_peak_rate_gbp,
        settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency,
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
    )
    return [
        (
            "sensor.ha_spark_v2l_power_w",
            f"{session.last_power_w:.0f}",
            {
                "friendly_name": "ha-spark V2L power",
                "unit_of_measurement": "W",
                "device_class": "power",
            },
        ),
        (
            "sensor.ha_spark_v2l_energy_kwh",
            f"{session.kwh_delivered:.2f}",
            {
                "friendly_name": "ha-spark V2L energy",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_v2l_net_saving_gbp",
            f"{net:.2f}",
            {
                "friendly_name": "ha-spark V2L net saving",
                "unit_of_measurement": "GBP",
                "device_class": "monetary",
                "avoided_gbp": round(avoided, 2),
                "refill_cost_gbp": round(refill, 2),
                "peak_power_w": round(session.peak_power_w, 0),
            },
        ),
    ]
