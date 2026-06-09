"""ha-spark's own home-load forecast — owns the pipeline, retiring HA templates.

`predict_home_load` is the seam consumed by the planner. Fallback chain:

1. v2 slot profile — medians per local half-hour slot from the SQLite consumption
   history (fed by `import-csv` / `pull-consumption`). Gives a 48-slot forecast.
2. v1 daily median of recent house-consumption totals from HA long-term statistics.
3. The configured baseline (`expected_load_kwh`).

Phase 5's trained model is expected to slot in behind the same interface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from ha_spark.config import Settings
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.profile import build_slot_profile, predict_day_slots
from ha_spark.energy.store import ConsumptionStore
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


async def predict_home_load(settings: Settings) -> LoadForecast:
    """Predict tomorrow's home load, per-slot when enough local history exists."""
    tz = load_timezone(settings.timezone)
    try:
        async with ConsumptionStore(settings.db_path) as store:
            since = datetime.now(UTC) - timedelta(days=settings.profile_history_days)
            intervals = await store.load_since(since)
        profile = build_slot_profile(intervals, tz, min_days=settings.profile_min_days)
        if profile is not None:
            tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
            slots = predict_day_slots(profile, tomorrow)
            return LoadForecast(
                total_kwh=sum(slots),
                slots=slots,
                source=f"slot profile ({profile.days_used}d half-hourly history)",
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
