"""ha-spark's own home-load forecast — owns the pipeline, retiring HA templates.

`predict_home_load` is the seam consumed by the planner. Fallback chain:

1. v2 slot profile — medians per local half-hour slot, built from hourly HA
   long-term statistics of the house-consumption sensor (true load: excludes
   battery charging and EV). Gives a 48-slot forecast.
2. v1 daily median of recent house-consumption totals from HA long-term statistics.
3. The configured baseline (`expected_load_kwh`).

The Octopus consumption store deliberately does NOT feed this model: Octopus
meter data is grid *import*, which the battery/solar have shaped for the whole
history — training on it teaches the planner "what the battery already did".
The store is kept for cost backtesting instead.

Phase 5's trained model is expected to slot in behind the same interface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from ha_spark.config import Settings
from ha_spark.energy.models import ConsumptionInterval, LoadForecast
from ha_spark.energy.profile import build_slot_profile, predict_day_slots
from ha_spark.ha.statistics import statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)


def load_timezone(name: str) -> ZoneInfo:
    """The configured local tz; UTC if tzdata is missing (degraded but functional)."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - e.g. no tzdata in a minimal container
        log.warning("Timezone %r unavailable; falling back to UTC", name)
        return ZoneInfo("UTC")


def _intervals_from_hourly_stats(rows: list[dict[str, Any]]) -> list[ConsumptionInterval]:
    """Split hourly stats rows into half-hour intervals (kWh halved, assumed uniform)."""
    intervals: list[ConsumptionInterval] = []
    for row in rows:
        change = row.get("change")
        raw_start = row.get("start")
        if change is None or raw_start is None or float(change) < 0:
            continue
        start = datetime.fromtimestamp(float(raw_start) / 1000, UTC)
        half_kwh = float(change) / 2
        for i in range(2):
            slot_start = start + timedelta(minutes=30 * i)
            intervals.append(
                ConsumptionInterval(
                    start=slot_start, end=slot_start + timedelta(minutes=30), kwh=half_kwh
                )
            )
    return intervals


async def predict_home_load(settings: Settings) -> LoadForecast:
    """Predict tomorrow's home load, per-slot when enough local history exists."""
    tz = load_timezone(settings.timezone)
    try:
        start = datetime.now(UTC) - timedelta(days=settings.profile_history_days)
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            settings.consumption_energy_entity,
            start,
            period="hour",
            timeout=settings.ha_timeout,
        )
        intervals = _intervals_from_hourly_stats(rows)
        profile = build_slot_profile(intervals, tz, min_days=settings.profile_min_days)
        if profile is not None:
            tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
            slots = predict_day_slots(profile, tomorrow)
            return LoadForecast(
                total_kwh=sum(slots),
                slots=slots,
                source=f"slot profile ({profile.days_used}d hourly house stats)",
            )
    except Exception as exc:  # noqa: BLE001 - the forecast must never crash the plan
        log.warning("Slot-profile forecast unavailable (%s); falling back", exc)
    total, source = await predict_home_load_kwh(settings)
    return LoadForecast(total_kwh=total, slots=None, source=source)


def _daily_totals(rows: list[dict[str, Any]]) -> list[float]:
    """Daily kWh from stats rows: prefer per-period ``change``, else diff ``sum``."""
    changes = [float(r["change"]) for r in rows if r.get("change") is not None]
    if changes:
        return [c for c in changes if c >= 0]
    cumulative = [float(r["sum"]) for r in rows if r.get("sum") is not None]
    if len(cumulative) >= 2:
        return [b - a for a, b in zip(cumulative, cumulative[1:], strict=False) if b - a >= 0]
    return []


async def predict_home_load_kwh(settings: Settings) -> tuple[float, str]:
    """Return (predicted tomorrow home-load kWh, human source description)."""
    start = datetime.now(UTC) - timedelta(days=settings.forecast_days + 1)
    try:
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            settings.consumption_energy_entity,
            start,
            period="day",
            timeout=settings.ha_timeout,
        )
        totals = _daily_totals(rows)
        if totals:
            return float(median(totals)), f"median of {len(totals)}d house consumption (stats)"
        log.warning(
            "No usable statistics rows for %s; using baseline",
            settings.consumption_energy_entity,
        )
    except Exception as exc:  # noqa: BLE001 - the forecast must never crash the plan
        log.warning("Load statistics unavailable (%s); using baseline", exc)
    return settings.expected_load_kwh, "configured baseline (stats unavailable)"
