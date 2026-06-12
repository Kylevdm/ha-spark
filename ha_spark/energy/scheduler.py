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
from datetime import date, datetime, time

from ha_spark.config import Settings
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.planner import _in_overnight_window as in_window
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs, parse_time
from ha_spark.energy.supply_guard import SupplyGuard
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


def should_run(now: datetime, run_time: time, last_run_date: date | None) -> bool:
    """True once per calendar day, at or after ``run_time`` local time."""
    return now.time() >= run_time and now.date() != last_run_date


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
    return plan


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
        await asyncio.sleep(poll_seconds)
