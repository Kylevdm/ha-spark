"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from ha_spark.energy.energy_ledger import LedgerSlot
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
