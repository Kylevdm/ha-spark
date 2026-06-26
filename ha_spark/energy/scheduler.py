"""Daily scheduled plan + apply loop.

`run_once` computes and applies a single charge plan (the same path as
`ha-spark plan --apply`). `run_forever` wakes once a minute and calls
`run_once` exactly once per local calendar day, at `settings.plan_run_time`.
A failed run is retried on the next tick (since `last_run_date` is left
unset) until it succeeds or the day rolls over.

When `grid_power_entity` is set, every tick inside the charge window also runs
the supply guard: throttle the battery's charge-current setpoint while
whole-house draw exceeds `supply_max_current_a`, restoring toward the plan's
current as headroom returns. Outside the window the timed-charge setpoint is
inert (and there is nothing else ha-spark can shed), so the guard stays quiet.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

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
from ha_spark.energy.chargers import charger_for
from ha_spark.energy.forecast import forecast_model_tag, load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargePlan, PlannerInputs
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.planner import _in_overnight_window as in_window
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.publish import publish_plan, publish_predictions, republish_last
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs, parse_time
from ha_spark.energy.supply_guard import SupplyGuard
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# How often the signal sampler records occupancy/heat-pump/temperature signals.
SIGNAL_SAMPLE_INTERVAL = timedelta(minutes=30)


def should_run(now: datetime, run_time: time, last_run_date: date | None) -> bool:
    """True once per calendar day, at or after ``run_time`` local time."""
    return now.time() >= run_time and now.date() != last_run_date


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


async def run_once(settings: Settings) -> ChargePlan:
    """Compute the charge plan, log it, and apply it per PROACTIVE_MODE."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        inputs, cfg, load_source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
        log.info("Charge plan:\n%s", format_plan(plan, load_source))
        intent = plan.charge_intent
        assert intent is not None  # planner always sets it
        lines = await charger_for(settings, rest).apply(intent)
        for line in lines:
            log.info(line)
        await publish_plan(rest, plan, settings)
    await _record_forecast(settings, plan, inputs, load_source)
    await _run_orchestrator(settings)
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


async def guard_tick(settings: Settings, target_w: float | None) -> float:
    """One supply-guard pass; returns the target charge power (W) used.

    No-op for inverters without a settable charge rate (the guard has nothing
    to throttle). A daemon (re)started mid-window has no plan yet; adopt the
    current charge-rate setpoint (W) as the restore target rather than guessing.
    """
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        charger = charger_for(settings, rest)
        if not charger.supports_live_rate:
            return target_w or 0.0
        if target_w is None:
            target_w = await charger.read_charge_rate()
            log.info("Supply guard: adopted current setpoint %.0f W as target", target_w)
        await SupplyGuard(settings, rest).tick(target_w)
    return target_w


async def _planned_rate_w(settings: Settings, plan: ChargePlan) -> float | None:
    """The plan's charge rate (W) for the active charger, or None if unset.

    ``charger_for``/``planned_rate_w`` perform no I/O; the rest client just
    satisfies the constructor and is closed straight away.
    """
    if plan.charge_intent is None:
        return None
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return charger_for(settings, rest).planned_rate_w(plan.charge_intent)


async def _charger_supports_live_rate(settings: Settings) -> bool:
    """Whether the configured inverter exposes a settable charge rate (no I/O)."""
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        return charger_for(settings, rest).supports_live_rate


async def run_forever(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Loop: run the plan once per day at ``settings.plan_run_time``.

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
    last_run_date: date | None = None
    target_w: float | None = None
    last_signal_at: datetime | None = None
    try:
        while True:
            settings = state.settings  # hot-reloaded by POST /api/config
            tz = load_timezone(settings.timezone)
            run_time = parse_time(settings.plan_run_time)
            window_start = parse_time(settings.charge_window_start)
            window_end = parse_time(settings.charge_window_end)
            if (settings.grid_power_entity, settings.inverter) != guard_cfg:
                guard_cfg = (settings.grid_power_entity, settings.inverter)
                guard_enabled = bool(settings.grid_power_entity) and (
                    await _charger_supports_live_rate(settings)
                )
            now = datetime.now(tz)
            if should_run(now, run_time, last_run_date):
                try:
                    plan = await run_once(settings)
                    state.set_plan(plan)
                    last_run_date = now.date()
                    target_w = await _planned_rate_w(settings, plan)
                except Exception:
                    log.exception("Scheduled plan run failed; will retry next tick")
            if guard_enabled and in_window(now.time(), window_start, window_end):
                try:
                    target_w = await guard_tick(settings, target_w)
                except Exception:
                    log.exception("Supply guard tick failed; will retry next tick")
            if last_signal_at is None or now - last_signal_at >= SIGNAL_SAMPLE_INTERVAL:
                try:
                    await sample_signals(settings, now)
                    last_signal_at = now
                except Exception:
                    log.exception("Signal sampling failed; will retry next tick")
            await asyncio.sleep(poll_seconds)
    finally:
        if serve_task is not None:
            await stop_server(server, serve_task)
        if port_server is not None and port_task is not None:
            await stop_server(port_server, port_task)
