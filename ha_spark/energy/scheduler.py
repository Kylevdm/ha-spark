"""Half-hourly scheduled plan + apply loop.

`run_once` computes and applies a single charge plan (the same path as
`ha-spark plan --apply`). `run_forever` wakes once a minute and calls
`run_once` once per local half-hour slot. A failed run is retried on the next
tick because the completed slot is not recorded until the run succeeds.

When `grid_power_entity` is set, every tick inside the charge window also runs
the supply guard: throttle the battery's charge-current setpoint while
whole-house draw exceeds `supply_max_current_a`, restoring toward the plan's
current as headroom returns. Outside the window the timed-charge setpoint is
inert (and there is nothing else ha-spark can shed), so the guard stays quiet.

Every tick also reconciles the inverter's hold state (`reconcile_tick`, #143):
a read-first pass that converges the whole-inverter enable on what the clock and
the dispatch holds imply. On the HA-entity path it re-reads the dispatch entity
each minute; the Octopus API path reuses the last plan's holds. It is deliberately independent of
`setpoint_changed` — a hold boundary is a clock event, not a plan change, and
dispatch bounds need not land on a half-hour.

Every tick also makes exactly one checked SoC observation (`soc_monitor_tick`,
#114): the daemon loop is the sole SoC observation cadence, and that one
measurement is reused by planning, device application, and guard work. The
first failed observation enters pending failure — new SoC-based programming
and charge-rate increases are blocked while valid supply-guard reductions
remain available — and consecutive failures are counted toward the configured
fallback-entry threshold (`soc_failure_threshold`, default 3).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import httpx
import uvicorn

from ha_spark.agent.auth import resolve_token
from ha_spark.api.server import (
    AGENT_PORT,
    INGRESS_PORT,
    OPTIONS_PATH,
    AppState,
    build_app,
    make_server,
    serve_in_background,
    stop_server,
)
from ha_spark.config import Settings
from ha_spark.devices import Capability, inverter_device
from ha_spark.energy.derived_base_load import (
    BACKFILL_NAME,
    BACKFILL_STATISTIC_ID,
    derive_specs_from_settings,
    rerive_trailing_window,
)
from ha_spark.energy.forecast import forecast_model_tag, load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargeIntent, ChargePlan, PlannerInputs
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.publish import (
    publish_plan,
    publish_predictions,
    publish_soc_integrity,
    republish_last,
)
from ha_spark.energy.report import format_plan
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.soc_monitor import SocMonitor, observe_soc
from ha_spark.energy.sources import parse_time, read_dispatches
from ha_spark.energy.supply_guard import SupplyGuard
from ha_spark.energy.tariff import _controlled_windows
from ha_spark.energy.tariff import _in_overnight_window as in_window
from ha_spark.energy.v2l import run_v2l_tick
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# How often the signal sampler records occupancy/heat-pump/temperature signals.
SIGNAL_SAMPLE_INTERVAL = timedelta(minutes=30)


REPLAN_INTERVAL = timedelta(minutes=30)


def _slot_start(now: datetime) -> datetime:
    """Return the local half-hour slot containing ``now``."""
    slot_minutes = int(REPLAN_INTERVAL.total_seconds() // 60)
    minute = now.minute - now.minute % slot_minutes
    return now.replace(minute=minute, second=0, microsecond=0)


def should_run(now: datetime, last_run_slot: datetime | None) -> bool:
    """True when the current local half-hour slot has not run successfully."""
    return _slot_start(now) != last_run_slot


def setpoint_changed(
    previous: ChargeIntent,
    current: ChargeIntent,
) -> bool:
    """Return whether a plan changes the command sent to an inverter.

    SoC is deliberately excluded. The daemon measures SoC every minute, so a
    fresh observation alone must not turn an unchanged plan into another device
    write.

    A pending export event is always a changed command because its device
    programming depends on the clock as well as the plan. Otherwise, a change
    in the charge target, window, holds, or export intent is a changed command.
    An unreadable Axle event is also always a changed command so the device gets
    a chance to preserve a verified resident export until the read recovers or
    the verified window ends (#148).
    """
    if not getattr(current, "export_trusted", True):
        return True
    if getattr(current, "export", None) is not None:
        return True
    return (
        previous.target_soc_pct != current.target_soc_pct
        or previous.window_start != current.window_start
        or previous.window_end != current.window_end
        or previous.holds != current.holds
        # The export value is an optional planner extension.  Comparing the
        # value itself catches changed/cancelled event windows while keeping
        # legacy inverter intents source-compatible.
        or getattr(previous, "export", None) != getattr(current, "export", None)
    )


async def _record_forecast(settings: Settings, plan: ChargePlan, inputs: PlannerInputs,
                            load_source: str) -> None:
    """Log tonight's forecast for tomorrow so `forecast-eval` can score it later."""
    tz = load_timezone(settings.timezone)
    target_date = (datetime.now(tz) + timedelta(days=1)).date()
    try:
        async with ForecastLedger(settings.db_path) as ledger:
            await ledger.record_forecast(
                datetime.now(UTC),
                target_date,
                forecast_model_tag(load_source),
                plan.load_kwh,
                inputs.load_slots,
                load_source,
            )
    except Exception:
        log.exception("Recording forecast failed")


async def run_once(
    settings: Settings,
    *,
    soc: SocMeasurement | None = None,
    previous_plan: ChargePlan | None = None,
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
) -> ChargePlan:
    """Compute the charge plan, log it, and apply it per PROACTIVE_MODE.

    ``soc`` is the daemon tick's checked measurement, reused as-is so the
    plan and its charge intent carry the exact observation the loop made
    (no independent reread); a failed measurement blocks real writes at the
    charger gate. Without one (CLI/agent callers) the plan makes its own
    observation through the same shared path.

    ``trusted_holds`` is the daemon's last trusted hold set, used by the
    reconcile when this plan's hold data is untrusted (#143 §3).
    """
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        run = await current_plan(settings, rest, soc=soc)
        plan, inputs, load_source = run.plan, run.inputs, run.load_source
        log.info("Charge plan:\n%s", format_plan(plan, load_source))
        intent = plan.charge_intent
        assert intent is not None  # planner always sets it
        device = inverter_device(settings, rest)
        # One reconcile pass before anything else, on every device-driving
        # caller (#143): an owner that abdicates when invoked from the CLI is
        # not one (ADR-0003), and the switch must have settled before `apply`
        # reads it as an export precondition. The daemon repeats this every
        # minute; here it also covers the skipped-setpoint path below, which
        # would otherwise leave the clock unserved for a whole slot.
        reconcile_intent = hold_reconcile_intent(intent, trusted_holds)
        lines = (
            await device.reconcile_holds(
                reconcile_intent, datetime.now(load_timezone(settings.timezone))
            )
            if reconcile_intent is not None
            else [UNTRUSTED_HOLDS_LINE]
        )
        if (
            previous_plan is not None
            and previous_plan.soc.ok
            and not setpoint_changed(
                previous_plan.charge_intent,
                intent,
            )
        ):
            lines.append("[SKIP] charge setpoint unchanged")
        else:
            lines.extend(await device.apply(intent))
        for line in lines:
            log.info(line)
        await publish_plan(rest, plan, settings)
    await _record_forecast(settings, plan, inputs, load_source)
    await _run_orchestrator(settings)
    await _run_derived_rerive(settings)
    return plan


async def _run_orchestrator(settings: Settings) -> None:
    """Compute proactive decisions for tomorrow and publish them (best-effort).

    Isolated so a failure here never aborts the daily run. Nothing is executed
    yet — decisions are advisory; this is the seam later proactivity hangs off.
    """
    try:
        decisions = await orchestrate(settings)
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await publish_predictions(rest, decisions, settings)
    except Exception:
        log.exception("Proactive orchestrator failed")


async def _run_derived_rerive(settings: Settings) -> None:
    """Re-derive the trailing 48h of base-load history (best-effort).

    No-op when grid import is unconfigured (the source-entity backfill path
    is still available via ``backfill-load --from``). A failure here must
    never block the scheduled plan run; it logs + reports so the operator can
    inspect the daemon log.
    """
    specs = derive_specs_from_settings(settings)
    if not specs.get("grid_import") or not specs["grid_import"].entity_id:
        return
    try:
        result = await rerive_trailing_window(
            settings,
            specs,
            statistic_id=BACKFILL_STATISTIC_ID,
            statistic_name=BACKFILL_NAME,
        )
    except Exception:
        log.exception("Derived base-load rerive failed; will retry next tick")
        return
    if result is None:
        return
    if result.rows_imported:
        log.info(
            "Derived base-load rerive: %d rows upserted (%s)",
            result.rows_imported,
            result.span,
        )
    if result.negative_clamped:
        log.warning(
            "Derived base-load rerive: %d hour(s) clamped to 0 — check invert flags",
            result.negative_clamped,
        )
    for note in result.degradation:
        log.info("Derived base-load rerive: %s", note)


async def sample_signals(settings: Settings, now: datetime) -> None:
    """Record occupancy/heat-pump/temperature signals for one sample tick.

    Each signal is independently best-effort: an unreadable entity logs a
    warning and is skipped, it never aborts the others or the daemon loop.
    """
    async with (
        HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest,
        ForecastLedger(settings.db_path) as ledger,
    ):
        person_entities = [e.strip() for e in settings.person_entities.split(",") if e.strip()]
        if person_entities:
            home = 0
            for entity in person_entities:
                try:
                    state = await rest.get_state(entity)
                except httpx.HTTPError as exc:
                    log.warning("Signal sampler: %s unreadable (%s)", entity, exc)
                    continue
                if state.state == "home":
                    home += 1
            await ledger.record_signal(now, "occupancy_home_frac", home / len(person_entities))

        if settings.heatpump_energy_entity:
            try:
                state = await rest.get_state(settings.heatpump_energy_entity)
                await ledger.record_signal(now, "heatpump_kwh", float(state.state))
            except (httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "Signal sampler: %s unreadable (%s)", settings.heatpump_energy_entity, exc
                )

        if settings.outdoor_weather_entity:
            try:
                state = await rest.get_state(settings.outdoor_weather_entity)
                temp = state.attributes.get("temperature")
                if temp is not None:
                    await ledger.record_signal(now, "temp_out_c", float(temp))
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                log.warning(
                    "Signal sampler: %s unreadable (%s)", settings.outdoor_weather_entity, exc
                )


async def guard_tick(
    settings: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
) -> float:
    """One supply-guard pass; returns the target charge power (W) used.

    No-op for inverters without a settable charge rate (the guard has nothing
    to throttle). A daemon (re)started mid-window has no plan yet; adopt the
    current charge-rate setpoint (W) as the restore target rather than guessing.
    ``soc`` is the tick's checked measurement: while it failed the guard can
    only reduce (the cap is applied inside ``SupplyGuard.tick``).
    """
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        charger = inverter_device(settings, rest)
        if Capability.CHARGE_RATE not in charger.capabilities:
            return target_w or 0.0
        if target_w is None:
            target_w = await charger.read_charge_rate()
            log.info("Supply guard: adopted current setpoint %.0f W as target", target_w)
        await SupplyGuard(settings, rest).tick(target_w, soc=soc)
    return target_w


UNTRUSTED_HOLDS_LINE = (
    "[SKIP] hold data untrusted and no trusted hold set yet; power switch left as-is"
)
UNREADABLE_DISPATCH_LINE = "[SKIP] dispatch entity unreadable; using the last trusted holds"


def hold_reconcile_intent(
    intent: ChargeIntent, trusted_holds: tuple[tuple[datetime, datetime], ...] | None
) -> ChargeIntent | None:
    """The intent a reconcile pass may act on, or ``None`` when it must do nothing.

    Degraded hold data is ignored, never believed (#143 §3). A trusted intent is
    used as-is. An untrusted one, whose empty holds mean "unreadable" rather than
    "no dispatch", is evaluated against the last trusted hold set instead, so a
    hold still ends on its own known end time: no expiry timer, no stuck ``Off``.
    With no trusted set yet (boot into a failed read, a hot reload, the CLI)
    there is no picture, and no picture never writes ``On``.
    """
    if intent.hold_trusted:
        return intent
    if trusted_holds is None:
        return None
    return replace(intent, holds=trusted_holds)


class ReconcileResult(NamedTuple):
    """One reconcile pass: its action lines, the caller's trusted hold set after it,
    and whether it relinquished control (so the next replan re-applies the plan)."""

    lines: list[str]
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None
    relinquished: bool = False


async def reconcile_tick(
    settings: Settings,
    plan: ChargePlan | None,
    now: datetime,
    *,
    previous: list[str] | None = None,
    trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None,
) -> ReconcileResult:
    """One per-minute power-switch reconcile pass. Never raises.

    The clock seam (#143). ``apply`` is a plan diff on the half-hourly replan
    cadence, but whether a dispatch hold is active *right now* is a function of
    the clock, and Octopus dispatch bounds are whatever HA reports — need not be
    half-hour aligned, and can open and close inside one slot. Driving the
    reconcile from here converges a sub-slot hold, a failed write, and an
    external change (the select flipped in the HA UI, a leftover automation)
    within a minute rather than at the next plan change.

    It is cheap by construction: ``reconcile_holds`` is read-first, so steady
    state is one *cached* ``GET`` of the select per minute and zero register
    writes. The cadence is not a new precedent — ``soc_monitor_tick`` and the
    writing ``guard_tick`` already run here.

    With no plan yet (boot, or ``last_plan`` cleared by a hot reload) this does
    nothing at all: no picture is not evidence that no hold is active, and
    writing ``On`` merely because a plan has not loaded would release a live
    dispatch on every restart.

    Returns the pass's action lines so the caller can pass them back as
    ``previous``: only lines that changed since the last pass are logged. A
    per-minute pass that logged unconditionally would put 1440 identical lines a
    day into the add-on log — most of them ``[SKIP] ... (already set)``, or in
    the default ``simulate`` mode a write that never happens — and bury the plan
    and guard activity an operator actually reads it for.

    ``trusted_holds`` is the caller's last trusted hold set, substituted when
    this plan's hold data is untrusted (see :func:`hold_reconcile_intent`).

    On the HA-entity dispatch path the pass re-reads the dispatch entity (#143
    §4) — a cached ``GET`` of what BottlecapDave's integration last polled —
    so a dispatch published *and* started inside one slot holds within a
    minute rather than being missed. The holds are folded through the same
    ``_controlled_windows`` the planner uses, so an overnight dispatch stays
    cheap charge coverage, never a hold. A trusted read replaces the returned
    trusted set; a failed one falls back to it. The ``octopus_intelligent``
    path keeps the last plan's holds: per-minute Kraken polling would be 30x
    the current rate against an API whose own client caps refreshes.

    Relinquishing control (#143 §5): once the clock is past every known hold end
    and the hold data is *still* untrusted, ha-spark has no picture left to steer
    by, so it writes the device's safe state instead of reconciling — and
    returns an empty trusted set so it fires once: the one blind write is the
    bound, and any retry goes through the read-first reconcile and ``apply``,
    which never write blind (register endurance). Inside a known hold an
    untrusted read never relinquishes, a trusted one never does, and with no
    known holds (``None`` or ``()``) there is nothing to relinquish from. This is
    the bound on the reconcile's refused blind release: no counter, no tunable.
    """
    if plan is None or plan.charge_intent is None:
        return ReconcileResult([], trusted_holds)
    relinquished = False
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            fresh = plan.charge_intent
            read_lines: list[str] = []
            if settings.tariff_provider != "octopus_intelligent" and settings.dispatch_entity:
                dispatches, trusted = await read_dispatches(settings, rest)
                if trusted:
                    trusted_holds = _controlled_windows(
                        dispatches, fresh.window_start, fresh.window_end
                    )
                    fresh = replace(fresh, holds=trusted_holds, hold_trusted=True)
                else:
                    fresh = replace(fresh, hold_trusted=False)
                    read_lines = [UNREADABLE_DISPATCH_LINE]
            intent = hold_reconcile_intent(fresh, trusted_holds)
            if intent is None:
                lines = [UNTRUSTED_HOLDS_LINE]
            elif (
                not fresh.hold_trusted
                and trusted_holds
                and now >= max(end for _, end in trusted_holds)
            ):
                log.warning(
                    "[WARNING] relinquishing control: past every known hold end and hold "
                    "data still untrusted; writing the inverter safe state"
                )
                relinquished = True
                trusted_holds = ()
                lines = read_lines + await inverter_device(settings, rest).write_safe_state()
            else:
                lines = read_lines + await inverter_device(settings, rest).reconcile_holds(
                    intent, now
                )
    except Exception:
        log.exception("Hold reconcile tick failed; will retry next minute")
        return ReconcileResult([], trusted_holds, relinquished)
    for line in lines:
        if previous is not None and line in previous:
            log.debug(line)
        else:
            log.info(line)
    return ReconcileResult(lines, trusted_holds, relinquished)


async def soc_monitor_tick(settings: Settings, monitor: SocMonitor) -> SocMeasurement | None:
    """One per-minute SoC observation: check, count once, publish. Never raises.

    The daemon loop's sole SoC observation cadence (#114): the returned
    measurement is the one every consumer this tick (planning, device
    application, guard work) must reuse. ``None`` when no ``soc_entity`` is
    configured (nothing to observe) or when the tick itself failed — callers
    then fall back to their own observation path and the next minute retries.
    """
    if not settings.soc_entity:
        return None
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            measurement = await observe_soc(settings, rest)
            snapshot = monitor.record(
                measurement, failure_threshold=settings.soc_failure_threshold
            )
            await publish_soc_integrity(rest, snapshot, settings)
            return measurement
    except Exception:
        log.exception("SoC monitor tick failed; will retry next minute")
        return None


async def _planned_rate_w(settings: Settings, plan: ChargePlan) -> float | None:
    """The plan's charge rate (W) for the active charger, or None if unset.

    ``inverter_device``/``planned_rate_w`` perform no I/O; the rest client just
    satisfies the constructor and is closed straight away.
    """
    if plan.charge_intent is None:
        return None
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return inverter_device(settings, rest).planned_rate_w(plan.charge_intent)


async def _charger_supports_live_rate(settings: Settings) -> bool:
    """Whether the configured inverter exposes a settable charge rate (no I/O)."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return Capability.CHARGE_RATE in inverter_device(settings, rest).capabilities


async def run_forever(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Loop: recompute and apply the plan once per local half-hour slot.

    Serves the add-on HTTP API (behind ingress) sharing an :class:`AppState`
    with this loop: ``POST /api/config`` rewrites the options and the loop picks
    up the reloaded settings on its next tick (hot reload, no restart).
    """
    state = AppState(settings=settings, options_path=OPTIONS_PATH)
    server = make_server(build_app(state), "0.0.0.0", INGRESS_PORT)  # noqa: S104 - ingress only
    serve_task: asyncio.Task[None] | None = None
    try:
        serve_task = await serve_in_background(server)
        log.info("HTTP API listening on :%d (ingress)", INGRESS_PORT)
    except Exception:
        log.exception("HTTP API failed to start; continuing without it")

    port_server: uvicorn.Server | None = None
    port_task: asyncio.Task[None] | None = None
    if settings.agent_surface == "on" and settings.agent_expose_port:
        token = resolve_token(settings)
        port_server = make_server(
            build_app(state, require_token=True, token=token), "0.0.0.0", AGENT_PORT  # noqa: S104
        )
        try:
            port_task = await serve_in_background(port_server)
            log.info("Agent surface listening on :%d (token-protected)", AGENT_PORT)
        except Exception:
            log.exception("Agent port failed to start; continuing")

    # Guard only inverters with a live charge rate (AlphaESS self-regulates);
    # re-checked only when the inverter or grid entity changes (it needs a client).
    guard_cfg = (settings.grid_power_entity, settings.inverter)
    guard_enabled = bool(settings.grid_power_entity) and await _charger_supports_live_rate(settings)
    if guard_enabled:
        log.info(
            "Supply guard enabled: watching %s (limit %g A)",
            settings.grid_power_entity,
            settings.supply_max_current_a,
        )
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            await republish_last(rest, settings)
    except Exception:
        log.exception("Republishing last known states failed")
    last_run_slot: datetime | None = None
    last_plan: ChargePlan | None = None
    # The previous reconcile pass's lines, so a per-minute pass logs only what
    # changed rather than the same line 1440 times a day.
    last_reconcile_lines: list[str] = []
    # The hold set from the last trusted dispatch read (#143 §3): a trusted plan,
    # or on the HA-entity path the per-minute reconcile's own read (§4). A failed
    # read's empty holds mean "unreadable", so the reconcile evaluates the clock
    # against this instead. `None` until a trusted read exists.
    last_trusted_holds: tuple[tuple[datetime, datetime], ...] | None = None
    # Set when a reconcile pass relinquished control (#143 §5): the safe state
    # overwrote Slot 1, so the next replan must apply even an unchanged plan.
    reapply = False
    last_settings = settings
    target_w: float | None = None
    last_signal_at: datetime | None = None
    monitor = SocMonitor.load(settings)
    try:
        while True:
            settings = state.settings  # hot-reloaded by POST /api/config
            if settings is not last_settings:
                # Never reuse a previous plan's command across a hot reload.
                last_plan = None
                last_trusted_holds = None
                target_w = None
                last_settings = settings
            tz = load_timezone(settings.timezone)
            window_start = parse_time(settings.charge_window_start)
            window_end = parse_time(settings.charge_window_end)
            if (settings.grid_power_entity, settings.inverter) != guard_cfg:
                guard_cfg = (settings.grid_power_entity, settings.inverter)
                guard_enabled = bool(settings.grid_power_entity) and (
                    await _charger_supports_live_rate(settings)
                )
            now = datetime.now(tz)
            # Sole SoC observation cadence (#114): one checked measurement per
            # minute in every operating state, reused by everything below.
            measurement = await soc_monitor_tick(settings, monitor)
            # The clock cadence (#143), independent of `setpoint_changed`: the
            # power switch converges within a minute, not at the next plan
            # change. Only on ticks that do not replan — `run_once` makes its own
            # pass with the freshly computed plan, and reconciling here first
            # would drive the switch to the *stale* plan's state and then
            # immediately back, two writes and a momentarily wrong whole-inverter
            # enable whenever a dispatch is announced or cancelled between slots.
            if not should_run(now, last_run_slot):
                reconciled = await reconcile_tick(
                    settings,
                    last_plan,
                    now,
                    previous=last_reconcile_lines,
                    trusted_holds=last_trusted_holds,
                )
                last_reconcile_lines, last_trusted_holds = reconciled[:2]
                reapply = reapply or reconciled.relinquished
            if should_run(now, last_run_slot):
                try:
                    plan = await run_once(
                        settings,
                        soc=measurement,
                        previous_plan=None if reapply else last_plan,
                        trusted_holds=last_trusted_holds,
                    )
                    state.set_plan(plan)
                    last_run_slot = _slot_start(now)
                    last_plan = plan
                    reapply = False
                    if plan.charge_intent is not None and plan.charge_intent.hold_trusted:
                        last_trusted_holds = plan.charge_intent.holds
                    if plan.soc.ok:
                        target_w = await _planned_rate_w(settings, plan)
                    else:
                        # The plan was blocked on an untrusted SoC: its rate was
                        # sized from soc_now == 0 and must not become the guard's
                        # restore target. The guard adopts the live setpoint
                        # instead (reductions only) until a plan is computed from
                        # a trusted measurement (#116/#117 add the recompute).
                        target_w = None
                except Exception:
                    log.exception("Scheduled plan run failed; will retry next tick")
            if guard_enabled and in_window(now.time(), window_start, window_end):
                try:
                    target_w = await guard_tick(settings, target_w, soc=measurement)
                except Exception:
                    log.exception("Supply guard tick failed; will retry next tick")
            if last_signal_at is None or now - last_signal_at >= SIGNAL_SAMPLE_INTERVAL:
                try:
                    await sample_signals(settings, now)
                    last_signal_at = now
                except Exception:
                    log.exception("Signal sampling failed; will retry next tick")
            if settings.v2l_power_entity:
                try:
                    await run_v2l_tick(settings, now)
                except Exception:
                    log.exception("V2L tick failed; will retry next tick")
            await asyncio.sleep(poll_seconds)
    finally:
        if serve_task is not None:
            await stop_server(server, serve_task)
        if port_server is not None and port_task is not None:
            await stop_server(port_server, port_task)
        # One best-effort relinquish on a clean shutdown (#143 §5). No plan means
        # ha-spark never steered, so there is nothing to hand back. Same rule as
        # the per-minute relinquish: never while a known hold end is still ahead,
        # or every restart mid-dispatch would release the hold and drain the
        # battery into the car until the next process holds it again (#143 §1).
        # SIGKILL/OOM never reach here; that is the runbook's (#131/#134).
        if last_plan is not None:
            try:
                shutdown_at = datetime.now(load_timezone(settings.timezone))
                if last_trusted_holds and shutdown_at < max(
                    end for _, end in last_trusted_holds
                ):
                    log.warning(
                        "Not relinquishing control on shutdown: a known dispatch hold "
                        "is still ahead"
                    )
                else:
                    async with HomeAssistantRest(
                        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
                    ) as rest:
                        log.warning("[WARNING] relinquishing control on shutdown")
                        for line in await inverter_device(settings, rest).write_safe_state():
                            log.info(line)
            except Exception:
                log.exception("Writing the safe state on shutdown failed")
