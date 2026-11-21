"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from ha_spark.energy.energy_ledger import LedgerSlot, RateEvidence
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR
from ha_spark.energy.tariff import HAPPY_HOUR_ALLOWANCE_KWH


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


PRICEABLE = frozenset({"boundary", "statistics_fill"})
_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class CostInputs:
    rates: RateEvidence
    grid_import_kwh: float
    grid_export_kwh: float
    house_load_kwh: float
    solar_kwh: float
    no_battery_import_kwh: float
    no_battery_export_kwh: float
    free_import_kwh: float
    no_battery_free_import_kwh: float
    axle_earnings: float


@dataclass(frozen=True)
class SlotCost:
    start: datetime
    reason: str | None = None
    inputs: CostInputs | None = None
    actual_cost: float | None = None
    no_battery_cost: float | None = None
    battery_savings: float | None = None


@dataclass(frozen=True)
class CostResult:
    slots: tuple[SlotCost, ...]
    priced_slots: int
    actual_cost: float | None
    no_battery_cost: float | None
    battery_savings: float | None

    @property
    def cost_coverage(self) -> float:
        return self.priced_slots / len(self.slots) if self.slots else 0.0


def _price_slot(bucket: StatsBucket, previous: SlotCost | None) -> SlotCost:
    row = bucket.slot
    start = bucket.start
    if row is None:
        return SlotCost(start, reason="missing_slot")
    import_rate, export_rate = row.import_rate, row.export_rate
    if import_rate is None or export_rate is None:
        return SlotCost(start, reason=row.exclusions[0] if row.exclusions else "rate_not_captured")
    house = row.house_load_kwh
    if house is None:
        return SlotCost(start, reason="missing_house_load")

    values: dict[str, float] = {}
    for name in COMPONENTS:
        if name == "v2l":  # Not used to derive house load.
            continue
        flow = row.components.get(name)
        if flow is not None and flow.kwh is not None and flow.provenance in PRICEABLE:
            values[name] = flow.kwh
        elif (flow is not None and flow.kwh is None and flow.provenance == "absent"
              and flow.reason == "not_configured"):
            values[name] = 0.0
        else:
            return SlotCost(start, reason=f"untrusted_{name}")
    imported, exported = values["grid_import"], values["grid_export"]
    solar = values["solar_generation"]
    baseline_import, baseline_export = max(0.0, house - solar), max(0.0, solar - house)

    free = baseline_free = 0.0
    if row.import_session == "free_electricity":
        free, baseline_free = imported, baseline_import
    elif row.import_session == "happy_hour":
        instant = start.astimezone(UTC)
        if not row.session_id or row.session_start is None or row.session_end is None:
            return SlotCost(start, reason="missing_happy_hour_booking")
        if row.session_suppressed_by == "no_purchase_room" and row.session_remaining_kwh is None:
            return SlotCost(start, reason="missing_happy_hour_allowance")
        elapsed = instant - row.session_start.astimezone(UTC)
        within_hour = elapsed % _HOUR
        if (elapsed < timedelta(0) or instant + HALF_HOUR > row.session_end.astimezone(UTC)
                or within_hour not in (timedelta(0), HALF_HOUR)):
            return SlotCost(start, reason="happy_hour_slot_not_aligned")
        remaining = baseline_remaining = HAPPY_HOUR_ALLOWANCE_KWH
        if within_hour:
            # The preceding absolute half-hour is the first slot in this booking hour.
            if (previous is None or previous.inputs is None
                    or previous.start.astimezone(UTC) != instant - HALF_HOUR):
                return SlotCost(start, reason="happy_hour_earlier_slot_unpriced")
            remaining -= previous.inputs.grid_import_kwh
            baseline_remaining -= previous.inputs.no_battery_import_kwh
        cap = row.session_remaining_kwh
        if cap is not None:
            # The snapshot may already account for prior slots; cap, don't subtract again.
            remaining = min(remaining, cap)
            baseline_remaining = min(baseline_remaining, cap)
        free = min(imported, max(0.0, remaining))
        baseline_free = min(baseline_import, max(0.0, baseline_remaining))

    axle = 0.0
    actual = (imported - free) * import_rate - exported * export_rate - axle
    baseline = (baseline_import - baseline_free) * import_rate - baseline_export * export_rate
    inputs = CostInputs(
        rates=row, grid_import_kwh=imported, grid_export_kwh=exported, house_load_kwh=house,
        solar_kwh=solar, no_battery_import_kwh=baseline_import,
        no_battery_export_kwh=baseline_export, free_import_kwh=free,
        no_battery_free_import_kwh=baseline_free, axle_earnings=axle,
    )
    return SlotCost(
        start, inputs=inputs, actual_cost=_total((actual,)),
        no_battery_cost=_total((baseline,)), battery_savings=_total((baseline - actual,)),
    )


def cost_stats(result: StatsResult) -> CostResult:
    """Price measured half-hours at captured rates, excluding standing charges."""
    slots: list[SlotCost] = []
    for bucket in result.buckets:
        slots.append(_price_slot(bucket, slots[-1] if slots else None))
    priced = [slot for slot in slots if slot.inputs is not None]

    def total(values: list[float | None]) -> float | None:
        # An overflow is unknown, not a gap to omit from an otherwise covered total.
        return None if None in values else _total(values)

    return CostResult(
        tuple(slots), len(priced), total([slot.actual_cost for slot in priced]),
        total([slot.no_battery_cost for slot in priced]),
        total([slot.battery_savings for slot in priced]),
    )
