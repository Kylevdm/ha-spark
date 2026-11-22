"""Read-only `/api/history` presentation for ledger statistics and the plan view's solar series.

The legacy solar series keeps its own coverage and forecast selection (#496).
Ledger statistics have separate energy and cost coverage.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, TypeVar

from ha_spark.energy.energy_ledger import LedgerSlot
from ha_spark.energy.evening_forecast import EveningForecastStore, SolarForecastSlot
from ha_spark.energy.ledger import read_forecast_history
from ha_spark.energy.models import ConsumptionInterval, ForecastHistoryRecord
from ha_spark.energy.stats import (
    RangeStats,
    RangeTotals,
    Resolution,
    StatsResult,
    compute_stats,
    load_accuracy,
    meter_reconciliation,
    provenance_counts,
    rejected_slots,
    solar_accuracy,
)
from ha_spark.energy.store import read_consumption
from ha_spark.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")

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
            "cost_coverage": totals.cost_coverage, "partial": totals.partial}


def add_statistics(
    payload: dict[str, Any], stats: RangeStats, resolution: Resolution,
    *, ledger_failed: bool, forecast_failed: bool, calendar: bool = False,
) -> dict[str, Any]:
    """Add ledger statistics without changing the plan view's solar fields."""
    if resolution != "slot":
        payload["buckets"] = [{"start": bucket.start.isoformat()} for bucket in stats.buckets]
        payload["unavailable"]["solar_forecast"] = "slot_resolution_only"
        payload["totals"] = {"solar_forecast_kwh": None, "solar_measured_kwh": None}
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
        if calendar and bucket.cost is not None:
            entry["cost_reason"] = bucket.cost.reason
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


@dataclass(frozen=True)
class StatisticsEvidence:
    evening: list[SolarForecastSlot] | None
    forecasts: list[ForecastHistoryRecord] | None
    meter: list[ConsumptionInterval] | None


async def _read(source: Awaitable[list[T]], warning: str) -> list[T] | None:
    try:
        return await source
    except Exception:  # noqa: BLE001 - a failed source does not hide the other series
        log.warning(warning)
        return None


async def read_statistics_evidence(
    db_path: str, start: datetime, end: datetime, tz: tzinfo,
) -> StatisticsEvidence:
    first = start.astimezone(tz).date()
    last = max(start, end - timedelta(microseconds=1)).astimezone(tz).date()
    return StatisticsEvidence(
        await _read(EveningForecastStore(db_path).slots(first, last + timedelta(days=1)),
                    "Evening-plan forecast read failed"),
        await _read(read_forecast_history(db_path, first, last),
                    "Load forecast history read failed"),
        await _read(read_consumption(db_path, start, end), "Meter history read failed"),
    )


def _finite_fields(fields: dict[str, Any]) -> dict[str, Any]:
    # Accuracy sums can overflow even when each stored input is finite.
    return {
        key: None if isinstance(value, float) and not math.isfinite(value) else value
        for key, value in fields.items()
    }


def add_range_details(
    payload: dict[str, Any], stats: RangeStats, rows: Sequence[LedgerSlot],
    start: datetime, end: datetime, tz: tzinfo, resolution: Resolution,
    evidence: StatisticsEvidence,
) -> None:
    result = compute_stats(rows, start, end, tz)
    if resolution == "slot":
        # The calendar Day view never reads the plan view's recorded forecast.
        payload["totals"] = {"solar_forecast_kwh": None, "solar_measured_kwh": None}
    if "statistics" not in payload["unavailable"]:
        # After a failed read the empty rows are not evidence of gaps.
        payload["provenance_counts"] = provenance_counts(result)
        payload["rejected_slots"] = rejected_slots(result)
        payload["axle"] = {
            "delivery_rate": stats.axle.delivery_rate,
            "earnings_by_day": [
                {"date": day.isoformat(), "earnings_gbp": earnings}
                for day, earnings in stats.axle_earnings_by_day
            ],
            "events": [
                {"start": outcome.event.start.astimezone(tz).isoformat(),
                 "end": outcome.event.end.astimezone(tz).isoformat(),
                 "outcome": outcome.outcome, "delivered_kwh": outcome.delivered_kwh,
                 "earnings_gbp": outcome.earnings_gbp}
                for outcome in stats.axle.events
            ],
        }
    unavailable = payload["unavailable"]
    try:
        _add_solar(payload, result, resolution, evidence.evening)
    except Exception:  # noqa: BLE001 - a malformed stored series loses only its own block
        payload.pop("solar_accuracy", None)
        log.warning("Evening-plan forecast read failed")
        unavailable["solar_accuracy"] = "evening_forecast_unreadable"
    try:
        _add_load(payload, result, tz, evidence.forecasts)
    except Exception:  # noqa: BLE001 - a malformed stored series loses only its own block
        payload.pop("load_accuracy", None)
        log.warning("Load forecast history read failed")
        unavailable["load_accuracy"] = "forecast_history_unreadable"
    try:
        _add_meter(payload, result, tz, evidence.meter)
    except Exception:  # noqa: BLE001 - a malformed stored series loses only its own block
        payload.pop("meter_reconciliation", None)
        log.warning("Meter history read failed")
        unavailable["meter_reconciliation"] = "meter_history_unreadable"


def _add_solar(
    payload: dict[str, Any], result: StatsResult, resolution: Resolution,
    evening: list[SolarForecastSlot] | None,
) -> None:
    if evening is None:
        payload["unavailable"]["solar_accuracy"] = "evening_forecast_unreadable"
    else:
        solar = solar_accuracy(result, evening)
        payload["solar_accuracy"] = {
            "mae_kwh": solar.mae_kwh, "bias_kwh": solar.bias_kwh,
            "scored_slots": solar.scored_slots, "total": len(solar.slots),
            "coverage": solar.coverage, "reason": solar.reason,
        }
        if resolution == "slot":
            payload["solar_accuracy"]["slots"] = [
                {"start": slot.start.isoformat(),
                 "p10_kwh": slot.forecast.p10_kwh if slot.forecast else None,
                 "p50_kwh": slot.forecast.p50_kwh if slot.forecast else None,
                 "p90_kwh": slot.forecast.p90_kwh if slot.forecast else None,
                 "measured_kwh": slot.measured_kwh, "error_kwh": slot.error_kwh,
                 "band_hit": slot.band_hit}
                for slot in solar.slots
            ]


def _add_load(
    payload: dict[str, Any], result: StatsResult, tz: tzinfo,
    forecasts: list[ForecastHistoryRecord] | None,
) -> None:
    if forecasts is None:
        payload["unavailable"]["load_accuracy"] = "forecast_history_unreadable"
    else:
        accuracy = load_accuracy(result, forecasts, tz)
        payload["load_accuracy"] = {
            "days": [_finite_fields({**asdict(day), "day": day.day.isoformat()})
                     for day in accuracy.days],
            "models": [_finite_fields(asdict(model)) for model in accuracy.models],
        }


def _add_meter(
    payload: dict[str, Any], result: StatsResult, tz: tzinfo,
    meter_rows: list[ConsumptionInterval] | None,
) -> None:
    if meter_rows is None:
        payload["unavailable"]["meter_reconciliation"] = "meter_history_unreadable"
    else:
        meter = meter_reconciliation(result, meter_rows, tz)
        payload["meter_reconciliation"] = {
            "by_day": {day.isoformat(): fraction for day, fraction in meter.by_day.items()},
            "range_fraction": meter.range_fraction, "covered_days": meter.covered_days,
        }
