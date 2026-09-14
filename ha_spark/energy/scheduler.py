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
from datetime import UTC, datetime, timedelta

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
from ha_spark.energy.sources import parse_time
from ha_spark.energy.supply_guard import SupplyGuard
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
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Return whether a plan changes the command sent to an inverter.

    SoC is deliberately excluded. The daemon measures SoC every minute, so a
    fresh observation alone must not turn an unchanged plan into another device
    write.

    ``since``/``now`` are when ``previous`` was applied and the current tick.
    The power-switch reconcile (#140) is a function of the clock as well as the
    plan, so crossing a hold boundary is a changed command even between
    identical intents — without this the reconcile would never run, because a
    hold ending is not a plan change. Omitting them compares the plans alone.

    A pending export event is always a changed command, for the same reason one
    step further on (#144). The Solis driver arms an export window only once its
    clock face next comes round at the event, which is a function of the clock,
    not of the plan — and an Axle event announced a day ahead produces an equal
    ``ExportIntent`` tick after tick. Comparing plans alone would skip every
    apply between announcement and event, so the window would never be
    programmed and the paid event would be missed outright. Re-applying is
    cheap: every device write is write-if-changed and read-back verified, so an
    unchanged program costs reads, not writes.
    """
    if getattr(current, "export", None) is not None:
        return True
    if (
        since is not None
        and now is not None
        and previous.hold_active(since) != current.hold_active(now)
    ):
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
    previous_at: datetime | None = None,
) -> ChargePlan:
    """Compute the charge plan, log it, and apply it per PROACTIVE_MODE.

    ``soc`` is the daemon tick's checked measurement, reused as-is so the
    plan and its charge intent carry the exact observation the loop made
    (no independent reread); a failed measurement blocks real writes at the
    charger gate. Without one (CLI/agent callers) the plan makes its own
    observation through the same shared path.
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
        lines = await device.reconcile_holds(
            intent, datetime.now(load_timezone(settings.timezone))
        )
        if (
            previous_plan is not None
            and previous_plan.soc.ok
            and not setpoint_changed(
                previous_plan.charge_intent,
                intent,
                since=previous_at,
                now=datetime.now(load_timezone(settings.timezone)),
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
    # When the previous run actually happened, not the slot it belonged to: the
    # power-switch reconcile is a function of the clock, and dispatch bounds are
    # whatever HA reports, so rounding this to the slot start can hide a hold
    # boundary crossed by a mid-slot run and leave the inverter held off (#140).
    last_run_at: datetime | None = None
    last_plan: ChargePlan | None = None
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
                last_run_at = None
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
            if should_run(now, last_run_slot):
                try:
                    plan = await run_once(
                        settings,
                        soc=measurement,
                        previous_plan=last_plan,
                        previous_at=last_run_at,
                    )
                    state.set_plan(plan)
                    last_run_slot = _slot_start(now)
                    last_run_at = now
                    last_plan = plan
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
