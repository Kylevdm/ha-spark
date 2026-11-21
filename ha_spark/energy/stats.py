"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING

from ha_spark.energy.energy_ledger import LedgerSlot
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR

if TYPE_CHECKING:
    from ha_spark.energy.evening_forecast import SolarForecastSlot


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
class SolarSlotAccuracy:
    start: datetime
    forecast: SolarForecastSlot | None
    measured_kwh: float | None
    error_kwh: float | None  # Forecast minus measured; positive means over-forecast.
    band_hit: bool | None


@dataclass(frozen=True)
class SolarAccuracy:
    slots: tuple[SolarSlotAccuracy, ...]
    scored_slots: int
    mae_kwh: float | None
    bias_kwh: float | None
    reason: str | None

    @property
    def coverage(self) -> float:
        return self.scored_slots / len(self.slots) if self.slots else 0.0


def solar_accuracy(
    result: StatsResult, forecast_slots: Iterable[SolarForecastSlot],
) -> SolarAccuracy:
    """Compare the evening plan with measured solar, independently of other flows."""
    by_start = {slot.start.astimezone(UTC): slot for slot in forecast_slots}
    slots: list[SolarSlotAccuracy] = []
    errors: list[float] = []
    has_evening_plan = False
    for bucket in result.buckets:
        forecast = by_start.get(bucket.start.astimezone(UTC))
        flow = bucket.slot.components.get("solar_generation") if bucket.slot is not None else None
        measured = flow.kwh if flow is not None and flow.provenance != "absent" else None
        if measured is not None and not math.isfinite(measured):
            measured = None
        error = None
        band_hit = None
        if forecast is not None:
            has_evening_plan = True
            if measured is not None:
                if forecast.p50_kwh is not None:
                    delta = forecast.p50_kwh - measured
                    if math.isfinite(delta):
                        error = delta
                        errors.append(delta)
                if forecast.p10_kwh is not None and forecast.p90_kwh is not None:
                    band_hit = forecast.p10_kwh <= measured <= forecast.p90_kwh
        slots.append(SolarSlotAccuracy(bucket.start, forecast, measured, error, band_hit))

    reason = None
    if not has_evening_plan:
        reason = "no evening plan"
    elif not errors:
        reason = "no comparable solar slots"
    absolute_total = _total(abs(error) for error in errors)
    signed_total = _total(errors)
    return SolarAccuracy(
        tuple(slots), len(errors),
        mae_kwh=absolute_total / len(errors) if absolute_total is not None else None,
        bias_kwh=signed_total / len(errors) if signed_total is not None else None,
        reason=reason,
    )
