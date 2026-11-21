"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from ha_spark.energy.energy_ledger import LedgerSlot
from ha_spark.energy.models import ConsumptionInterval
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
class Reconciliation:
    by_day: dict[date, float | None]
    range_fraction: float | None
    covered_days: int


def _import_difference(ledger: float, meter: float) -> float | None:
    if meter <= 0 or not (math.isfinite(ledger) and math.isfinite(meter)):
        return None
    fraction = (ledger - meter) / meter
    return fraction if math.isfinite(fraction) else None


def meter_reconciliation(
    result: StatsResult, meter_intervals: Iterable[ConsumptionInterval], tz: tzinfo,
) -> Reconciliation:
    """Compare measured grid import with billed import over jointly covered household days."""
    billed: dict[datetime, float] = {}
    for row in meter_intervals:
        if row.start.utcoffset() is None or row.end.utcoffset() is None:
            continue
        start, end = row.start.astimezone(UTC), row.end.astimezone(UTC)
        if end - start == HALF_HOUR and math.isfinite(row.kwh) and row.kwh >= 0:
            billed[start] = row.kwh

    paired: dict[date, dict[datetime, tuple[float, float]]] = {}
    for bucket in result.buckets:
        start = bucket.start.astimezone(UTC)
        day = start.astimezone(tz).date()
        day_slots = paired.setdefault(day, {})
        flow = bucket.slot.components.get("grid_import") if bucket.slot is not None else None
        if flow is not None and flow.provenance != "absent" and flow.kwh is not None:
            if start in billed:
                day_slots[start] = flow.kwh, billed[start]

    by_day: dict[date, float | None] = {}
    ledger_totals, meter_totals = [], []
    for day, slots in paired.items():
        start = datetime.combine(day, time.min, tz).astimezone(UTC)
        end = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(UTC)
        by_day[day] = None
        # Compare absolute slots, not wall-clock times (which repeat at the autumn change).
        if len(slots) != (end - start) // HALF_HOUR:
            continue
        if any(not start <= at < end or (at - start) % HALF_HOUR for at in slots):
            continue
        ledger = sum(pair[0] for pair in slots.values())
        meter = sum(pair[1] for pair in slots.values())
        by_day[day] = _import_difference(ledger, meter)
        ledger_totals.append(ledger)
        meter_totals.append(meter)
    return Reconciliation(
        by_day, _import_difference(sum(ledger_totals), sum(meter_totals)), len(ledger_totals),
    )
