"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from ha_spark.energy.energy_ledger import LedgerSlot
from ha_spark.energy.models import ForecastHistoryRecord
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR


@dataclass(frozen=True)
class StatsBucket:
    start: datetime  # Household clock, with offset to distinguish repeated slots.
    slot: LedgerSlot | None


@dataclass(frozen=True)
class EnergyTotals:
    components_kwh: dict[str, float | None]
    house_load_kwh: float | None
    base_load_kwh: float | None


@dataclass(frozen=True)
class StatsResult:
    buckets: tuple[StatsBucket, ...]
    totals: EnergyTotals
    covered_slots: int

    @property
    def energy_coverage(self) -> float:
        return self.covered_slots / len(self.buckets) if self.buckets else 0.0


def _total(values: Iterable[float | None]) -> float | None:
    measured = [value for value in values if value is not None]
    total = sum(measured)
    return total if measured and math.isfinite(total) else None


def compute_stats(
    rows: Iterable[LedgerSlot], start: datetime, end: datetime, tz: tzinfo,
) -> StatsResult:
    """Compute slot buckets and covered totals for an aligned span [start, end)."""
    cursor, end = start.astimezone(UTC), end.astimezone(UTC)
    by_start = {row.slot_start.astimezone(UTC): row for row in rows}
    buckets: list[StatsBucket] = []
    covered: list[LedgerSlot] = []
    while cursor < end:
        row = by_start.get(cursor)
        buckets.append(StatsBucket(cursor.astimezone(tz), row))
        if row is not None and row.house_load_kwh is not None and row.base_load_kwh is not None:
            covered.append(row)
        cursor += HALF_HOUR

    totals = EnergyTotals(
        components_kwh={name: _total(
            row.components[name].kwh for row in covered if name in row.components
        ) for name in COMPONENTS},
        house_load_kwh=_total(row.house_load_kwh for row in covered),
        base_load_kwh=_total(row.base_load_kwh for row in covered),
    )
    return StatsResult(tuple(buckets), totals, len(covered))


@dataclass(frozen=True)
class LoadDayAccuracy:
    """One household day's forecast total scored against derived base load."""

    day: date
    model: str
    forecast_kwh: float
    actual_kwh: float | None  # None unless every slot of the day is covered
    error_kwh: float | None
    median_forecast_kwh: float | None
    median_error_kwh: float | None
    covered_slots: int
    total_slots: int

    @property
    def coverage(self) -> float:
        return self.covered_slots / self.total_slots if self.total_slots else 0.0

    @property
    def complete(self) -> bool:
        return self.error_kwh is not None


@dataclass(frozen=True)
class LoadModelAccuracy:
    """Per-model daily error and per-slot shape error over a range."""

    model: str
    days: int
    daily_mae_kwh: float | None
    shape_slots: int
    shape_mae_kwh: float | None


@dataclass(frozen=True)
class LoadAccuracy:
    """Per-day load errors and per-slot shape errors, with explicit coverage."""

    days: tuple[LoadDayAccuracy, ...]
    models: tuple[LoadModelAccuracy, ...]


def _forecast_for_day(
    day: date, forecasts: Iterable[ForecastHistoryRecord], tz: tzinfo,
) -> ForecastHistoryRecord | None:
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    driving = [
        forecast for forecast in forecasts
        if forecast.drove_plan and forecast.target_date == day
        and forecast.issued_at.astimezone(UTC) < start
    ]
    return max(driving, key=lambda forecast: forecast.issued_at) if driving else None


def _day_slot_starts(day: date, tz: tzinfo) -> list[datetime]:
    start = datetime.combine(day, time.min, tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
    count = int((end - start).total_seconds() / HALF_HOUR.total_seconds())
    return [start + i * HALF_HOUR for i in range(count)]


def load_accuracy(
    result: StatsResult, forecasts: Iterable[ForecastHistoryRecord], tz: tzinfo,
) -> LoadAccuracy:
    """Score recorded forecasts for each day against the ledger's base load.

    The driving forecast is the latest revision issued before the household day
    starts, as readiness selects it. A daily error needs every slot measured;
    shape error uses only covered slots and the forecast's local slot-of-day.
    """
    forecast_rows = list(forecasts)
    buckets_by_day: dict[date, list[StatsBucket]] = {}
    by_instant = {bucket.start.astimezone(UTC): bucket for bucket in result.buckets}
    for bucket in result.buckets:
        buckets_by_day.setdefault(bucket.start.astimezone(tz).date(), []).append(bucket)

    days: list[LoadDayAccuracy] = []
    daily_errors: dict[str, list[float]] = {}
    shape_errors: dict[str, list[float]] = {}

    for day in sorted(buckets_by_day):
        forecast = _forecast_for_day(day, forecast_rows, tz)
        if forecast is None:
            continue
        expected = _day_slot_starts(day, tz)
        rows = [by_instant.get(instant) for instant in expected]
        covered = [
            bucket.slot for bucket in rows
            if bucket is not None and bucket.slot is not None
            and bucket.slot.base_load_kwh is not None
        ]
        actual = sum(slot.base_load_kwh for slot in covered if slot.base_load_kwh is not None)
        complete = len(covered) == len(expected)
        error = (
            abs(forecast.total_kwh - actual)
            if complete and math.isfinite(forecast.total_kwh) else None
        )
        median_error = (
            abs(forecast.median_total_kwh - actual)
            if complete and forecast.median_total_kwh is not None
            and math.isfinite(forecast.median_total_kwh) else None
        )
        days.append(LoadDayAccuracy(
            day=day, model=forecast.model, forecast_kwh=forecast.total_kwh,
            actual_kwh=actual if complete else None, error_kwh=error,
            median_forecast_kwh=forecast.median_total_kwh,
            median_error_kwh=median_error, covered_slots=len(covered),
            total_slots=len(expected),
        ))
        daily_errors.setdefault(forecast.model, [])
        shape_errors.setdefault(forecast.model, [])
        if error is not None:
            daily_errors[forecast.model].append(error)
        if forecast.model != "median":
            daily_errors.setdefault("median", [])
            shape_errors.setdefault("median", [])
            if median_error is not None:
                daily_errors["median"].append(median_error)

        for bucket in buckets_by_day[day]:
            slot = bucket.slot
            if slot is None or slot.base_load_kwh is None:
                continue
            local = bucket.start.astimezone(tz)
            index = local.hour * 2 + local.minute // 30
            if forecast.slots is not None and index < len(forecast.slots):
                predicted = forecast.slots[index]
                if math.isfinite(predicted):
                    shape_errors[forecast.model].append(
                        abs(predicted - slot.base_load_kwh)
                    )
            if forecast.model != "median" and forecast.median_slots is not None:
                if index < len(forecast.median_slots):
                    predicted = forecast.median_slots[index]
                    if math.isfinite(predicted):
                        shape_errors["median"].append(
                            abs(predicted - slot.base_load_kwh)
                        )

    models = tuple(
        LoadModelAccuracy(
            model=model,
            days=len(daily_errors.get(model, [])),
            daily_mae_kwh=(
                sum(daily_errors[model]) / len(daily_errors[model])
                if daily_errors.get(model) else None
            ),
            shape_slots=len(shape_errors.get(model, [])),
            shape_mae_kwh=(
                sum(shape_errors[model]) / len(shape_errors[model])
                if shape_errors.get(model) else None
            ),
        )
        for model in {**daily_errors, **shape_errors}
    )
    return LoadAccuracy(tuple(days), models)
