"""Reconciliation is a separate statistic over measured ledger and meter days."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.models import ConsumptionInterval
from ha_spark.energy.stats import compute_stats, meter_reconciliation

SLOT = timedelta(minutes=30)


def readings(
    start: datetime, count: int, grid: float, billed: float,
) -> tuple[list[LedgerSlot], list[ConsumptionInterval]]:
    instants = [start + SLOT * index for index in range(count)]
    return (
        [LedgerSlot(slot_start=instant, components={
            "grid_import": ComponentFlow(provenance="distributed", kwh=grid),
        }) for instant in instants],
        [ConsumptionInterval(instant, instant + SLOT, billed) for instant in instants],
    )


def test_reconciliation_range_uses_only_days_with_both_sides_covered() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    day = timedelta(days=1)
    rows, meter = [], []
    for offset, (grid, billed) in enumerate([(2.0, 1.0), (3.0, 4.0), (100.0, 1.0), (1.0, 100.0)]):
        ledger_day, meter_day = readings(start + offset * day, 48, grid, billed)
        rows.extend(ledger_day if offset != 3 else ledger_day[:-1])
        meter.extend(meter_day if offset != 2 else meter_day[:-1])
    stats = compute_stats(rows, start, start + 4 * day, UTC)
    result = meter_reconciliation(stats, meter, UTC)
    assert result.by_day == {
        date(2026, 6, 1): 1.0, date(2026, 6, 2): -0.25,
        date(2026, 6, 3): None, date(2026, 6, 4): None,
    }
    assert result.covered_days == 2
    assert result.range_fraction == pytest.approx(0.0)  # Weighted by kWh, not by day.
    assert stats.totals.components_kwh["grid_import"] is None  # Meter never fills energy totals.


def test_reconciliation_with_zero_meter_import_is_unknown_but_covered() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows, meter = readings(start, 48, 1.0, 0.0)
    stats = compute_stats(rows, start, end, UTC)
    result = meter_reconciliation(stats, meter, UTC)
    assert result.by_day == {start.date(): None}
    assert result.range_fraction is None
    assert result.covered_days == 1


@pytest.mark.parametrize(("start", "day", "slots"), [
    (datetime(2026, 3, 29, tzinfo=UTC), date(2026, 3, 29), 46),
    (datetime(2026, 10, 24, 23, tzinfo=UTC), date(2026, 10, 25), 50),
])
def test_reconciliation_uses_every_real_half_hour_on_dst_days(
    start: datetime, day: date, slots: int,
) -> None:
    zone = ZoneInfo("Europe/London")
    end = start + slots * SLOT
    rows, meter = readings(start, slots, 1.25, 1.0)
    stats = compute_stats(rows, start, end, zone)
    assert meter_reconciliation(stats, meter, zone).by_day == {day: 0.25}
    assert meter_reconciliation(stats, meter[:-1], zone).by_day == {day: None}


def test_reconciliation_does_not_count_partial_days_or_absent_import() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows, meter = readings(start, 48, 1.0, 1.0)
    partial = compute_stats(rows, start + SLOT, end, UTC)
    assert meter_reconciliation(partial, meter, UTC).covered_days == 0
    rows[0] = LedgerSlot(slot_start=start, components={
        "grid_import": ComponentFlow(provenance="absent", kwh=1.0),
    })
    stats = compute_stats(rows, start, end, UTC)
    assert meter_reconciliation(stats, meter, UTC).by_day == {start.date(): None}
