"""ha-spark's own home-load forecast — owns the pipeline, retiring HA templates.

v1: median of recent daily house-consumption totals from HA long-term statistics.
Falls back to a configured baseline when statistics are unavailable. The interface
(`predict_home_load_kwh`) is the seam for the v2 trained 30-min model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from ha_spark.config import Settings
from ha_spark.ha.statistics import statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)


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
