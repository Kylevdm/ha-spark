"""Pure `/api/history` presentation for ledger statistics and the plan view's solar series.

The legacy solar series keeps its own coverage and forecast selection (#496).
Ledger statistics have separate energy and cost coverage.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from ha_spark.energy.stats import RangeStats, RangeTotals, Resolution

# One history bucket: 30 real minutes, whatever the wall clock does that night.
SLOT_MINUTES = 30


def bucket_starts(midnight: datetime, to: datetime) -> list[datetime]:
    """Absolute half-hours from local midnight (UTC) up to ``to``, exclusive."""
    step = timedelta(minutes=SLOT_MINUTES)
    starts: list[datetime] = []
    at = midnight.astimezone(UTC)
    while at < to:
        starts.append(at)
        at += step
    return starts


def history_payload(
    starts: Sequence[datetime],
    recorded: Mapping[datetime, float],
    tz: tzinfo,
    measured_reason: str | None,
    measured: Mapping[datetime, float] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Buckets, totals and completed-half-hour coverage from the same values."""
    measured = measured or {}
    buckets: list[dict[str, Any]] = []
    total = 0.0
    measured_total = 0.0
    completed = trustworthy = 0
    for start in starts:
        value = recorded.get(start)
        if value is not None:
            total += value
        measurement = measured.get(start) if measured_reason is None else None
        if measurement is not None:
            measured_total += measurement
        if now is not None and start + timedelta(minutes=30) <= now:
            completed += 1
            trustworthy += measurement is not None
        buckets.append({
            "start": start.astimezone(tz).isoformat(),
            "solar_forecast_kwh": value,
            "solar_measured_kwh": measurement,
        })
    return {
        "buckets": buckets,
        # Finite values can still sum past float range; an overflowed total is unknown.
        "totals": {"solar_forecast_kwh": total if math.isfinite(total) else None,
                   "solar_measured_kwh": (measured_total if measured_reason is None
                                          and math.isfinite(measured_total) else None)},
        "coverage": (trustworthy / completed
                     if completed and measured_reason is None else None),
        "unavailable": {"solar_measured": measured_reason} if measured_reason else {},
    }


def _totals_payload(totals: RangeTotals) -> dict[str, Any]:
    payload = asdict(totals)
    return {**payload.pop("energy"), **payload, "energy_coverage": totals.energy_coverage,
            "cost_coverage": totals.cost_coverage}


def add_statistics(
    payload: dict[str, Any], stats: RangeStats, resolution: Resolution,
    *, ledger_failed: bool, forecast_failed: bool,
) -> dict[str, Any]:
    """Add ledger statistics without changing the plan view's solar fields."""
    if resolution != "slot":
        payload["buckets"] = [{"start": bucket.start.isoformat()} for bucket in stats.buckets]
        payload["unavailable"]["solar_forecast"] = "slot_resolution_only"
    if ledger_failed:
        payload["unavailable"]["statistics"] = "ledger_unreadable"
    if forecast_failed:
        payload["unavailable"]["solar_forecast"] = "forecast_unreadable"
    for entry, bucket in zip(payload["buckets"], stats.buckets, strict=True):
        entry.update(_totals_payload(bucket.totals))
        if bucket.slot is not None:
            entry["flows"] = {
                name: {"kwh": flow.kwh, "provenance": flow.provenance}
                for name in bucket.totals.energy.components_kwh
                if (flow := bucket.slot.components.get(name)) is not None
            }
            entry["negative_clamped"] = bucket.slot.negative_clamped
        if bucket.cost is not None and (inputs := bucket.cost.inputs) is not None:
            entry["cost_inputs"] = {
                "import_rate": inputs.rates.import_rate, "export_rate": inputs.rates.export_rate,
                "import_session": inputs.rates.import_session,
                "grid_import_kwh": inputs.grid_import_kwh,
                "grid_export_kwh": inputs.grid_export_kwh,
                "house_load_kwh": inputs.house_load_kwh, "solar_kwh": inputs.solar_kwh,
                "free_import_kwh": inputs.free_import_kwh,
                "no_battery_import_kwh": (inputs.no_battery_import_kwh
                                          if math.isfinite(inputs.no_battery_import_kwh) else None),
                "no_battery_export_kwh": (inputs.no_battery_export_kwh
                                          if math.isfinite(inputs.no_battery_export_kwh) else None),
                "no_battery_free_import_kwh": inputs.no_battery_free_import_kwh,
                "axle_earnings": inputs.axle_earnings,
            }
    payload["resolution"] = resolution
    payload["range_totals"] = _totals_payload(stats.totals)
    return payload
