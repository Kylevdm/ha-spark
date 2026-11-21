"""Pure statistics on half-hour rows; absent measurements never count as zero."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.readiness_measurements import day_bounds
from ha_spark.energy.stats import compute_stats

TZ = ZoneInfo("Europe/London")
START = datetime(2026, 11, 1, tzinfo=UTC)
HALF_HOUR = timedelta(minutes=30)


@pytest.mark.parametrize(("day", "count"), [
    (date(2026, 2, 1), 48),
    (date(2026, 3, 29), 46),
    (date(2026, 10, 25), 50),
])
def test_household_day_has_real_half_hours(day, count):
    start, end = day_bounds(day, TZ.key)
    result = compute_stats([], start, end, TZ)
    assert len(result.buckets) == count
    assert all(bucket.start.date() == day for bucket in result.buckets)
    assert [b.start.astimezone(UTC) for b in result.buckets] == [
        start + i * HALF_HOUR for i in range(count)
    ]
    assert result.energy_coverage == 0
    assert result.totals.house_load_kwh is None


@pytest.mark.parametrize(("values", "expected"), [((0.0, 2.0), 2.0), ((1e308, 1e308), None)])
def test_totals_only_use_covered_slots_and_reject_overflow(values, expected):
    rows = [LedgerSlot(
        slot_start=START + i * HALF_HOUR,
        components={"grid_import": ComponentFlow(kwh=kwh, provenance="boundary")},
        house_load_kwh=kwh, base_load_kwh=kwh, import_rate=0.2, export_rate=0.05,
    ) for i, kwh in enumerate(values)]
    rows.append(LedgerSlot(
        slot_start=START + 2 * HALF_HOUR,
        components={"grid_import": ComponentFlow(kwh=4, provenance="boundary")},
    ))
    result = compute_stats(rows, START, START + 4 * HALF_HOUR, TZ)
    assert result.energy_coverage == 0.5
    assert result.covered_slots == 2
    assert result.buckets[0].slot == rows[0]
    assert result.buckets[2].slot == rows[2]
    assert result.buckets[3].slot is None
    assert result.totals.house_load_kwh == expected
    assert result.totals.base_load_kwh == expected
    assert result.totals.components_kwh["grid_import"] == expected
    assert result.totals.components_kwh["grid_export"] is None
