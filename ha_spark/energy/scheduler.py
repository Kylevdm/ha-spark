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

from ha_spark.config import Settings
from ha_spark.energy import habits
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.context import ContextStore
from ha_spark.energy.forecast import forecast_model_tag, load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargePlan, PlannerInputs
from ha_spark.energy.planner import _in_overnight_window as in_window
from ha_spark.energy.planner import compute_plan
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
        lines = await SolisCharger(settings, rest).apply(plan)
        for line in lines:
            log.info(line)
    await _record_forecast(settings, plan, inputs, load_source)
    await _log_habit_predictions(settings)
    return plan


async def _log_habit_predictions(settings: Settings) -> None:
    """Request and log the habit API's advisory predictions for tomorrow (6E).

    Always logged; the ``PROACTIVE_MODE`` it would honour is shown. Nothing is
    executed yet — this is the orchestrator seam later proactivity hangs off.
    """
    tz = load_timezone(settings.timezone)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    try:
        since = datetime.now(UTC) - timedelta(days=settings.profile_history_days)
        async with ForecastLedger(settings.db_path) as ledger:
            occ_samples = await ledger.signal_history("occupancy_home_frac", since)
        async with ContextStore(settings.db_path) as store:
            active = await store.active_on(tomorrow)
        ctx = habits.HabitContext(
            target_date=tomorrow,
            predicted_occupancy=habits.predict_occupancy(occ_samples, tomorrow, tz),
            away_active=any(e.kind == "away" for e in active),
            learned_away_factor=None,
        )
        for action in habits.predict_actions(ctx):
            log.info(
                "Habit prediction [mode=%s] %s (%.0f%% confidence): %s",
                settings.proactive_mode, action.action,
                action.confidence * 100, action.reason,
            )
    except Exception:
        log.exception("Habit prediction logging failed")


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


async def guard_tick(settings: Settings, target_a: float | None) -> float:
    """One supply-guard pass; returns the target current used (adopted if None).

    A daemon (re)started mid-window has no plan yet; adopt the current
    charge-current setpoint as the restore target rather than guessing.
    """
    async with HomeAssistantRest(
        settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
    ) as rest:
        if target_a is None:
            state = await rest.get_state(settings.charge_current_entity)
            target_a = float(state.state)
            log.info("Supply guard: adopted current setpoint %g A as target", target_a)
        await SupplyGuard(settings, rest).tick(target_a)
    return target_a


async def run_forever(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Loop: run the plan once per day at ``settings.plan_run_time``."""
    tz = load_timezone(settings.timezone)
    run_time = parse_time(settings.plan_run_time)
    window_start = parse_time(settings.charge_window_start)
    window_end = parse_time(settings.charge_window_end)
    if settings.grid_power_entity:
        log.info(
            "Supply guard enabled: watching %s (limit %g A)",
            settings.grid_power_entity,
            settings.supply_max_current_a,
        )
    last_run_date: date | None = None
    target_a: float | None = None
    last_signal_at: datetime | None = None
    while True:
        now = datetime.now(tz)
        if should_run(now, run_time, last_run_date):
            try:
                plan = await run_once(settings)
                last_run_date = now.date()
                target_a = plan.overnight_current_a
            except Exception:
                log.exception("Scheduled plan run failed; will retry next tick")
        if settings.grid_power_entity and in_window(now.time(), window_start, window_end):
            try:
                target_a = await guard_tick(settings, target_a)
            except Exception:
                log.exception("Supply guard tick failed; will retry next tick")
        if last_signal_at is None or now - last_signal_at >= SIGNAL_SAMPLE_INTERVAL:
            try:
                await sample_signals(settings, now)
                last_signal_at = now
            except Exception:
                log.exception("Signal sampling failed; will retry next tick")
        await asyncio.sleep(poll_seconds)
