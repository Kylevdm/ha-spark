"""Onboarding: build house-load history from an existing HA statistic.

`backfill_load` reads hourly long-term statistics for a user-selected source
entity (a power sensor like the zappi home-consumption sensor, or an energy
sensor), converts them to hourly kWh, and imports them as the external
statistic ``ha_spark:house_load`` via the recorder WS API. Pointing
``consumption_energy_entity`` at that id then feeds the slot-profile forecast
without any helper entity or HA database surgery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ha_spark.config import Settings
from ha_spark.ha.statistics import import_statistics, list_statistic_ids, statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)

BACKFILL_STATISTIC_ID = "ha_spark:house_load"
BACKFILL_NAME = "ha-spark house load (backfilled)"
# How far back the backfill pulls source history.
BACKFILL_LOOKBACK_DAYS = 730

# kWh per hourly row: power sensors use the hourly mean (mean power x 1h),
# energy sensors use the per-hour change.
_POWER_FACTORS = {"W": 0.001, "kW": 1.0}
_ENERGY_FACTORS = {"kWh": 1.0, "Wh": 0.001}
SUPPORTED_UNITS = frozenset(_POWER_FACTORS) | frozenset(_ENERGY_FACTORS)


def statistic_unit(meta: dict[str, Any]) -> str:
    """The source unit from a ``list_statistic_ids`` row (HA-version tolerant)."""
    return str(
        meta.get("statistics_unit_of_measurement") or meta.get("unit_of_measurement") or ""
    )


def hourly_kwh_from_stats(
    rows: list[dict[str, Any]], unit: str, entity: str
) -> list[tuple[datetime, float]]:
    """Convert hourly statistics rows to (hour start UTC, kWh) per the source unit."""
    if unit in _POWER_FACTORS:
        factor, key = _POWER_FACTORS[unit], "mean"
    elif unit in _ENERGY_FACTORS:
        factor, key = _ENERGY_FACTORS[unit], "change"
    else:
        raise ValueError(
            f"Unsupported unit {unit!r} for {entity}; "
            f"need one of {sorted(SUPPORTED_UNITS)}"
        )
    hourly: list[tuple[datetime, float]] = []
    for row in rows:
        raw_start, value = row.get("start"), row.get(key)
        if raw_start is None or value is None:
            continue
        start = datetime.fromtimestamp(float(raw_start) / 1000, UTC)
        hourly.append((start, max(0.0, round(float(value) * factor, 4))))
    return hourly


def to_import_stats(hourly: list[tuple[datetime, float]]) -> list[dict[str, Any]]:
    """Rows for ``recorder/import_statistics``: running cumulative sum, time order."""
    cumulative = 0.0
    stats: list[dict[str, Any]] = []
    for start, kwh in sorted(hourly):
        cumulative += kwh
        stats.append({"start": start.isoformat(), "state": kwh, "sum": round(cumulative, 4)})
    return stats


async def backfill_load(settings: Settings, source_entity: str) -> tuple[int, str]:
    """Backfill ``ha_spark:house_load`` from ``source_entity``'s hourly statistics.

    Returns (rows imported, human date-range summary). Raises ``ValueError``
    when the source has no statistics or an unsupported unit. Re-runs are
    idempotent: the recorder upserts by (statistic_id, start).
    """
    ws_url, token = settings.ha_websocket_url, settings.auth_token
    metas = await list_statistic_ids(ws_url, token, timeout=settings.ha_timeout)
    meta = next((m for m in metas if m.get("statistic_id") == source_entity), None)
    if meta is None:
        raise ValueError(
            f"{source_entity} has no long-term statistics in HA "
            "(use `ha-spark backfill-load --list` to see candidates)"
        )
    unit = statistic_unit(meta)
    start = datetime.now(UTC) - timedelta(days=BACKFILL_LOOKBACK_DAYS)
    rows = await statistics_during_period(
        ws_url, token, source_entity, start, period="hour", timeout=settings.ha_timeout
    )
    hourly = hourly_kwh_from_stats(rows, unit, source_entity)
    if not hourly:
        raise ValueError(f"No usable hourly statistics rows for {source_entity}")
    stats = to_import_stats(hourly)
    # A long history is one big WS message; allow the import more headroom.
    await import_statistics(
        ws_url,
        token,
        statistic_id=BACKFILL_STATISTIC_ID,
        name=BACKFILL_NAME,
        unit_of_measurement="kWh",
        stats=stats,
        timeout=max(settings.ha_timeout, 60.0),
    )
    first, _ = min(hourly)
    last, _ = max(hourly)
    return len(stats), f"{first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M} UTC"
