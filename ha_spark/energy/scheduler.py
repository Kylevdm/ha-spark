"""Daily scheduled plan + apply loop.

`run_once` computes and applies a single charge plan (the same path as
`ha-spark plan --apply`). `run_forever` wakes once a minute and calls
`run_once` exactly once per local calendar day, at `settings.plan_run_time`.
A failed run is retried on the next tick (since `last_run_date` is left
unset) until it succeeds or the day rolls over.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time

from ha_spark.config import Settings
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs, parse_time
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


def should_run(now: datetime, run_time: time, last_run_date: date | None) -> bool:
    """True once per calendar day, at or after ``run_time`` local time."""
    return now.time() >= run_time and now.date() != last_run_date


async def run_once(settings: Settings) -> None:
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


async def run_forever(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Loop: run the plan once per day at ``settings.plan_run_time``."""
    tz = load_timezone(settings.timezone)
    run_time = parse_time(settings.plan_run_time)
    last_run_date: date | None = None
    while True:
        now = datetime.now(tz)
        if should_run(now, run_time, last_run_date):
            try:
                await run_once(settings)
                last_run_date = now.date()
            except Exception:
                log.exception("Scheduled plan run failed; will retry next tick")
        await asyncio.sleep(poll_seconds)
