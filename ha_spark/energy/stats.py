"""Pure half-hour statistics: aggregate measured energy, retaining gaps and provenance."""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import chain
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from ha_spark.energy.energy_ledger import LedgerSlot, RateEvidence
from ha_spark.energy.models import ConsumptionInterval, ForecastHistoryRecord
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR, day_bounds
from ha_spark.energy.tariff import HAPPY_HOUR_ALLOWANCE_KWH

if TYPE_CHECKING:
    from ha_spark.energy.axle_events import AxleEvent
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
    while cursor < end:
        buckets.append(StatsBucket(cursor.astimezone(tz), by_start.get(cursor)))
        cursor += HALF_HOUR
    return _energise(tuple(buckets))


def _energise(buckets: tuple[StatsBucket, ...]) -> StatsResult:
    covered = [row for bucket in buckets if (row := bucket.slot) is not None
               and row.house_load_kwh is not None and row.base_load_kwh is not None]
    totals = EnergyTotals(
        components_kwh={name: _total(
            row.components[name].kwh for row in covered if name in row.components
        ) for name in COMPONENTS},
        house_load_kwh=_total(row.house_load_kwh for row in covered),
        base_load_kwh=_total(row.base_load_kwh for row in covered),
    )
    return StatsResult(buckets, totals, len(covered))


PRICEABLE = frozenset({"boundary", "statistics_fill"})
_HOUR = timedelta(hours=1)


AxleStatus = Literal["unknown", "cancelled", "delivered", "partial", "missed"]


@dataclass(frozen=True)
class AxleOutcome:
    event: AxleEvent
    outcome: AxleStatus = "unknown"
    delivered_kwh: float | None = None
    earnings_gbp: float | None = None


@dataclass(frozen=True)
class AxleSummary:
    events: tuple[AxleOutcome, ...]

    @property
    def delivery_rate(self) -> float | None:
        scored = [row for row in self.events if row.outcome in ("delivered", "partial", "missed")]
        return sum(row.outcome == "delivered" for row in scored) / len(scored) if scored else None


_NO_AXLE = AxleSummary(())


def axle_outcomes(events: Iterable[AxleEvent], rows: Iterable[LedgerSlot]) -> AxleSummary:
    """Score accepted windows from complete measured slots, including any later fills.

    Ledger rows represent closed slots. A partial edge, a missing slot or any
    unmeasured cost component in one slot makes the whole window ineligible
    for an estimate (owner ruling, 2026-11-21).
    """
    by_start = {row.slot_start.astimezone(UTC): row for row in rows}
    outcomes: list[AxleOutcome] = []
    for event in events:
        if event.cancelled_at is not None:
            outcomes.append(AxleOutcome(event, "cancelled", earnings_gbp=0.0))
            continue
        start, end = event.start.astimezone(UTC), event.end.astimezone(UTC)
        count, edge = divmod(end - start, HALF_HOUR)
        delivered = 0.0
        if not edge and count <= len(by_start):
            for i in range(count):
                row = by_start.get(start + i * HALF_HOUR)
                # A gap in any measured component, not only grid export, leaves no estimate.
                values = _measured(row) if row is not None else "missing_slot"
                export = row.components.get("grid_export") if row is not None else None
                # An unconfigured export meter prices as 0 kWh but is no delivery evidence.
                if isinstance(values, str) or export is None or export.provenance not in PRICEABLE:
                    break
                delivered += values["grid_export"]
            else:
                if math.isfinite(delivered):
                    outcome: AxleStatus = "unknown"
                    if event.committed_kwh:
                        fraction = delivered / event.committed_kwh
                        # Decimal kWh readings can land one float rounding step below a threshold.
                        if fraction >= math.nextafter(0.9, 0.0):
                            outcome = "delivered"
                        elif fraction >= math.nextafter(0.1, 0.0):
                            outcome = "partial"
                        else:
                            outcome = "missed"
                    earnings = delivered * event.rate_gbp_kwh
                    outcomes.append(AxleOutcome(
                        event, outcome, delivered, earnings if math.isfinite(earnings) else None,
                    ))
                    continue
        outcomes.append(AxleOutcome(event))
    return AxleSummary(tuple(outcomes))


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
    # The slot's Axle credit, priced or not; None means its event has no estimate.
    axle_credit: float | None = 0.0


@dataclass(frozen=True)
class CostResult:
    slots: tuple[SlotCost, ...]
    priced_slots: int
    actual_cost: float | None
    no_battery_cost: float | None
    battery_savings: float | None
    axle: AxleSummary = _NO_AXLE

    @property
    def axle_earnings_by_day(self) -> dict[date, float | None]:
        """Each household day's estimated Axle credit; unknown when any slot lacks an estimate."""
        by_day: dict[date, list[float | None]] = {}
        for slot in self.slots:
            by_day.setdefault(slot.start.date(), []).append(slot.axle_credit)
        return {day: None if None in values else _total(values) for day, values in by_day.items()}

    @property
    def cost_coverage(self) -> float:
        return self.priced_slots / len(self.slots) if self.slots else 0.0


def _measured(row: LedgerSlot) -> dict[str, float] | str:
    """The cost components of a slot, or the reason one is a ledger gap."""
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
            return f"untrusted_{name}"
    return values


def _price_slot(bucket: StatsBucket, previous: SlotCost | None, axle: float | None) -> SlotCost:
    row = bucket.slot
    start = bucket.start
    if axle is None:
        return SlotCost(start, reason="axle_no_estimate")
    if row is None:
        return SlotCost(start, reason="missing_slot")
    import_rate, export_rate = row.import_rate, row.export_rate
    if import_rate is None or export_rate is None:
        return SlotCost(start, reason=row.exclusions[0] if row.exclusions else "rate_not_captured")
    house = row.house_load_kwh
    if house is None:
        return SlotCost(start, reason="missing_house_load")

    values = _measured(row)
    if isinstance(values, str):
        return SlotCost(start, reason=values)
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


def cost_stats(
    result: StatsResult, events: Sequence[AxleEvent] = (), *, event_rows: Iterable[LedgerSlot] = (),
) -> CostResult:
    """Price measured half-hours, with supplemental rows for events crossing the range edges."""
    starts = [bucket.start.astimezone(UTC) for bucket in result.buckets]
    credits: list[float | None] = [0.0] * len(starts)
    rows = (bucket.slot for bucket in result.buckets if bucket.slot is not None)
    axle = axle_outcomes(events, chain(event_rows, rows)) if events else _NO_AXLE
    for outcome in axle.events:
        if outcome.outcome == "cancelled":
            continue
        event = outcome.event
        start, end = event.start.astimezone(UTC), event.end.astimezone(UTC)
        first = bisect_left(starts, start)
        if first and start - starts[first - 1] < HALF_HOUR:
            first -= 1  # Include a partial leading edge in the unpriced span.
        for i in range(first, bisect_left(starts, end)):
            if outcome.earnings_gbp is None:
                credits[i] = None
            elif (credit := credits[i]) is not None:
                row = result.buckets[i].slot
                export = row.components.get("grid_export") if row is not None else None
                credits[i] = (
                    _total((credit + export.kwh * event.rate_gbp_kwh,))
                    if export is not None and export.kwh is not None else None
                )

    slots: list[SlotCost] = []
    for bucket, credit in zip(result.buckets, credits, strict=True):
        cost = _price_slot(bucket, slots[-1] if slots else None, credit)
        slots.append(replace(cost, axle_credit=credit))
    return _sum_costs(tuple(slots), axle)


def _sum_costs(slots: tuple[SlotCost, ...], axle: AxleSummary = _NO_AXLE) -> CostResult:
    priced = [slot for slot in slots if slot.inputs is not None]

    def total(values: list[float | None]) -> float | None:
        # An overflow is unknown, not a gap to omit from an otherwise covered total.
        return None if None in values else _total(values)

    return CostResult(
        slots, len(priced), total([slot.actual_cost for slot in priced]),
        total([slot.no_battery_cost for slot in priced]),
        total([slot.battery_savings for slot in priced]), axle,
    )


Resolution = Literal["slot", "day", "month"]


def _next_month(day: date) -> date:
    return day.replace(day=1, month=day.month + 1) if day.month < 12 else date(day.year + 1, 1, 1)


def clamp_end(start: datetime, end: datetime, now: datetime) -> datetime:
    """Return ``end`` capped at ``now`` (all timezone-aware), in UTC.

    Every statistics surface shows a period only up to now, as ``/api/history``
    does with ``min(stop, now)``: a period containing now ends there, whatever
    date in it was asked for, and a past period is unchanged. A period that has
    not started keeps its end (``/api/history`` rejects it before this point);
    one starting exactly now is empty, as it is there.
    """
    end = end.astimezone(UTC)
    return min(end, now.astimezone(UTC)) if start <= now else end


def range_bounds(
    kind: str, at: date, tz: ZoneInfo, *, first_slot: datetime | None = None,
) -> tuple[datetime, datetime]:
    """UTC calendar bounds; weeks start on Monday. All ends after the anchor day."""
    if kind == "day":
        return day_bounds(at, tz.key)
    if kind == "week":
        first = at - timedelta(days=at.weekday())
        stop = first + timedelta(days=7)
    elif kind == "month":
        first, stop = at.replace(day=1), _next_month(at)
    elif kind == "year":
        first, stop = date(at.year, 1, 1), date(at.year + 1, 1, 1)
    elif kind == "all":
        earliest = first_slot.astimezone(tz).date() if first_slot is not None else at
        first, stop = min(earliest, at).replace(day=1), at + timedelta(days=1)
    else:
        raise ValueError("unknown statistics range")
    return (datetime.combine(first, time.min, tz).astimezone(UTC),
            datetime.combine(stop, time.min, tz).astimezone(UTC))


@dataclass(frozen=True)
class RangeTotals:
    energy: EnergyTotals
    actual_cost: float | None
    no_battery_cost: float | None
    battery_savings: float | None
    covered_slots: int
    priced_slots: int
    total_slots: int
    lower_resolution: bool

    @property
    def energy_coverage(self) -> float:
        return self.covered_slots / self.total_slots if self.total_slots else 0.0

    @property
    def cost_coverage(self) -> float:
        return self.priced_slots / self.total_slots if self.total_slots else 0.0


@dataclass(frozen=True)
class RangeBucket:
    start: datetime
    totals: RangeTotals
    slot: LedgerSlot | None = None
    cost: SlotCost | None = None


@dataclass(frozen=True)
class RangeStats:
    buckets: tuple[RangeBucket, ...]
    totals: RangeTotals
    axle: AxleSummary = _NO_AXLE
    axle_earnings_by_day: tuple[tuple[date, float | None], ...] = ()


def _range_totals(energy: StatsResult, costs: CostResult, count: int) -> RangeTotals:
    return RangeTotals(
        energy.totals, costs.actual_cost, costs.no_battery_cost, costs.battery_savings,
        energy.covered_slots, costs.priced_slots, count,
        any(flow.provenance == "hourly_backfill" for b in energy.buckets if b.slot is not None
            for flow in b.slot.components.values()),
    )


def summarise(
    rows: Iterable[LedgerSlot], start: datetime, end: datetime, tz: tzinfo,
    resolution: Resolution, events: Sequence[AxleEvent] = (), *,
    event_rows: Iterable[LedgerSlot] = (),
) -> RangeStats:
    """Summarise an aligned span [start, end) on the household half-hour grid.

    Price before grouping, so a Happy Hour is not restarted at a bucket boundary.
    For calendar buckets count missing slots without materialising them.
    """
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    rows = list(rows)
    # The slot before `start` is pricing context only (a Happy Hour's first half-hour),
    # so a narrower request prices a slot as a wider one does. It is never counted.
    before = start - HALF_HOUR
    context = next((row for row in rows if row.slot_start.astimezone(UTC) == before), None)

    def index(at: datetime) -> int:
        return -((start - at) // HALF_HOUR)  # Round up to the next slot on the grid.

    if resolution == "slot":
        energy = compute_stats(rows, start, end, tz)
    else:
        by_start: dict[datetime, StatsBucket] = {}
        for row in rows:
            at = row.slot_start.astimezone(UTC)
            if start <= at < end:
                local = at.astimezone(tz)
                midnight = datetime.combine(local.date(), time.min, tz).astimezone(UTC)
                # Anchor on the row's household day: a historical offset change
                # need not be a multiple of half an hour.
                if not (at - midnight) % HALF_HOUR:
                    by_start[at] = StatsBucket(local, row)
        energy = _energise(tuple(by_start[at] for at in sorted(by_start)))
    if context is None:
        costs = cost_stats(energy, events, event_rows=event_rows)
    else:
        lead = StatsBucket(before.astimezone(tz), context)
        priced = cost_stats(StatsResult((lead, *energy.buckets), energy.totals,
                                        energy.covered_slots), events, event_rows=event_rows)
        costs = _sum_costs(priced.slots[1:], priced.axle)
    buckets: list[RangeBucket] = []
    if resolution == "slot":
        for bucket, cost in zip(energy.buckets, costs.slots, strict=True):
            totals = _range_totals(_energise((bucket,)), _sum_costs((cost,)), 1)
            buckets.append(RangeBucket(bucket.start, totals, bucket.slot, cost))
    else:
        cursor, left = start, 0
        while cursor < end:
            day = cursor.astimezone(tz).date()
            if resolution == "month":
                day = day.replace(day=1)
            label = datetime.combine(day, time.min, tz)
            next_day = day + timedelta(days=1) if resolution == "day" else _next_month(day)
            stop = datetime.combine(next_day, time.min, tz).astimezone(UTC)
            if stop <= cursor:
                # A rollback across midnight repeats the day: end at the later midnight.
                stop = datetime.combine(next_day, time.min, tz).replace(fold=1).astimezone(UTC)
            stop = min(end, stop)
            right = left
            while right < len(energy.buckets) and costs.slots[right].start.astimezone(UTC) < stop:
                right += 1
            totals = _range_totals(_energise(energy.buckets[left:right]),
                                   _sum_costs(costs.slots[left:right]), index(stop) - index(cursor))
            buckets.append(RangeBucket(label, totals))
            cursor, left = stop, right
    return RangeStats(
        tuple(buckets), _range_totals(energy, costs, max(0, index(end))), costs.axle,
        tuple(costs.axle_earnings_by_day.items()),
    )


def provenance_counts(result: StatsResult) -> dict[str, int]:
    """Count slots by grid import provenance, distinguishing missing rows."""
    counts: dict[str, int] = {}
    for bucket in result.buckets:
        if bucket.slot is None:
            key = "gap"
        else:
            flow = bucket.slot.components.get("grid_import")
            key = flow.provenance if flow is not None else "absent"
        counts[key] = counts.get(key, 0) + 1
    return counts


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
