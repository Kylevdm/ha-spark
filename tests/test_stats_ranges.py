"""Calendar statistics on constructed ledger rows."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.readiness_measurements import COMPONENTS
from ha_spark.energy.stats import clamp_end, range_bounds, summarise

LONDON = ZoneInfo("Europe/London")


@pytest.mark.parametrize(("kind", "first", "stop"), [
    ("week", date(2026, 8, 31), date(2026, 9, 7)),
    ("month", date(2026, 9, 1), date(2026, 10, 1)),
    ("year", date(2026, 1, 1), date(2027, 1, 1)),
])
def test_range_bounds_follow_household_calendar(kind, first, stop):
    start, end = range_bounds(kind, date(2026, 9, 1), LONDON)
    assert start == datetime.combine(first, datetime.min.time(), LONDON).astimezone(UTC)
    assert end == datetime.combine(stop, datetime.min.time(), LONDON).astimezone(UTC)


def test_all_starts_in_earliest_slots_household_month():
    earliest = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)  # September on the household clock
    start, end = range_bounds("all", date(2026, 10, 3), LONDON, first_slot=earliest)
    assert start == datetime(2026, 8, 31, 23, tzinfo=UTC)
    assert end == datetime(2026, 10, 3, 23, tzinfo=UTC)


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_range_buckets_keep_real_dst_slots(day, count):
    start, end = range_bounds("day", day, LONDON)
    slots = summarise([], start, end, LONDON, "slot")
    assert len(slots.buckets) == count
    assert len({b.start.astimezone(UTC) for b in slots.buckets}) == count
    assert summarise([], start, end, LONDON, "day").totals.total_slots == count
    assert summarise([], start, end, LONDON, "month").totals.total_slots == count


@pytest.mark.parametrize(("day", "count", "slots"), [
    (date(2026, 3, 29), 23, 46), (date(2026, 10, 25), 25, 50),
])
def test_hour_buckets_follow_the_household_clock(day, count, slots):
    start, end = range_bounds("day", day, LONDON)
    hours = summarise([], start, end, LONDON, "hour")
    assert len(hours.buckets) == count
    assert hours.totals.total_slots == slots
    labels = [bucket.start.isoformat() for bucket in hours.buckets]
    assert len(set(labels)) == count  # the repeated hour labels twice, distinctly
    if day == date(2026, 10, 25):  # the repeated hour appears with both offsets
        assert "2026-10-25T01:00:00+01:00" in labels
        assert "2026-10-25T01:00:00+00:00" in labels
    else:  # the skipped hour never appears
        assert "2026-03-29T01:00:00+00:00" not in labels


def test_hour_buckets_price_and_total_like_slot_resolution():
    start = datetime(2026, 3, 30, 23, tzinfo=UTC)  # March 31 on the household clock
    end = start + timedelta(hours=1)
    rows = [priced_row(start, 2), priced_row(start + timedelta(minutes=30), 3)]
    hours = summarise(rows, start, end, LONDON, "hour")
    assert hours.totals == summarise(rows, start, end, LONDON, "slot").totals
    assert len(hours.buckets) == 1
    hour = hours.buckets[0]
    assert hour.start == datetime(2026, 3, 31, 0, tzinfo=LONDON)
    assert hour.totals.energy.components_kwh["grid_import"] == 5
    assert hour.totals.covered_slots == 2
    assert hour.totals.actual_cost == 1.25
    assert not hour.totals.partial


def test_day_and_hour_buckets_mark_partial_coverage():
    start = datetime(2026, 3, 30, 23, tzinfo=UTC)
    end = start + timedelta(hours=1)
    rows = [priced_row(start, 2)]  # one of the range's two half-hours
    for resolution in ("hour", "day"):
        stats = summarise(rows, start, end, LONDON, resolution)
        assert stats.buckets[0].totals.covered_slots == 1
        assert stats.buckets[0].totals.total_slots == 2
        assert stats.buckets[0].totals.partial


def priced_row(start, imported, provenance="boundary"):
    return LedgerSlot(
        slot_start=start, house_load_kwh=1, base_load_kwh=1, import_rate=.25, export_rate=.125,
        components={name: ComponentFlow(
            kwh=imported if name == "grid_import" else 0, provenance=provenance,
        ) for name in COMPONENTS},
    )


def test_long_month_range_keeps_rows_after_household_offset_changes():
    start, _ = range_bounds("year", date(1847, 1, 1), LONDON)  # Started on local mean time.
    end, _ = range_bounds("year", date(1849, 1, 1), LONDON)
    when = datetime(1848, 1, 1, tzinfo=UTC)
    stats = summarise([priced_row(when, 2)], start, end, LONDON, "month")
    assert stats.totals.energy.components_kwh["grid_import"] == 2
    assert stats.totals.actual_cost == .5


@pytest.mark.parametrize("resolution", ["day", "month"])
def test_bucket_and_range_totals_sum_covered_slots(resolution):
    start = datetime(2026, 3, 30, 23, tzinfo=UTC)  # March 31 on the household clock
    next_day = start + timedelta(days=1)
    end = next_day + timedelta(hours=1)
    rows = [priced_row(start, 2), priced_row(next_day, 3, "hourly_backfill"),
            priced_row(next_day + timedelta(minutes=30), 4)]
    slots = summarise(rows, start, end, LONDON, "slot")
    aggregate = summarise(rows, start, end, LONDON, resolution)
    assert aggregate.totals == slots.totals
    assert [bucket.start.day for bucket in aggregate.buckets] == (
        [31, 1] if resolution == "day" else [1, 1]
    )
    assert [b.totals.energy.components_kwh["grid_import"] for b in aggregate.buckets] == [2, 7]
    assert [b.totals.actual_cost for b in aggregate.buckets] == [.5, 1]
    assert [b.totals.no_battery_cost for b in aggregate.buckets] == [.25, .25]
    assert [b.totals.battery_savings for b in aggregate.buckets] == [-.25, -.75]
    assert aggregate.totals.energy_coverage == 3 / 50
    assert aggregate.totals.cost_coverage == 2 / 50
    assert aggregate.buckets[1].totals.lower_resolution


def test_happy_hour_before_the_range_prices_its_second_half_hour_alike():
    # A booking 23:30-00:30 (winter, so household time is UTC): a request starting at
    # midnight prices 00:00 as the wider request does, without counting 23:30.
    session = datetime(2026, 1, 11, 23, 30, tzinfo=UTC)
    rows = [priced_row(session + i * timedelta(minutes=30), 10).model_copy(update={
        "import_session": "happy_hour", "session_id": "hh", "session_start": session,
        "session_end": session + timedelta(hours=1),
    }) for i in range(2)]
    midnight = session + timedelta(minutes=30)
    end = midnight + timedelta(minutes=30)
    wide = summarise(rows, session, end, LONDON, "slot")
    for resolution in ("slot", "day", "month"):
        narrow = summarise(rows, midnight, end, LONDON, resolution)
        assert narrow.totals.total_slots == 1
        assert narrow.totals.priced_slots == 1
        assert narrow.totals.energy.components_kwh["grid_import"] == 10
        assert narrow.totals.actual_cost == wide.buckets[1].totals.actual_cost


def test_rollback_across_midnight_never_gives_a_negative_bucket():
    # Newfoundland's 1988 double-daylight rollback at 00:01 repeated part of 29 October.
    tz = ZoneInfo("America/St_Johns")
    start = datetime(1988, 10, 30, 2, tzinfo=UTC)
    stats = summarise([], start, start + timedelta(minutes=30), tz, "day")
    assert [b.totals.total_slots for b in stats.buckets] == [1]
    assert stats.buckets[0].start.date() == date(1988, 10, 29)


def test_clamp_end_matches_history_at_every_edge() -> None:
    """``min(stop, now)`` as ``/api/history`` does, once the period has started (#620)."""
    start = datetime(2026, 9, 1, tzinfo=LONDON)
    end = datetime(2026, 9, 2, tzinfo=LONDON)
    noon = datetime(2026, 9, 1, 12, tzinfo=LONDON)
    assert clamp_end(start, end, noon) == noon.astimezone(UTC)
    assert clamp_end(start, end, start) == start.astimezone(UTC)  # starts now: empty
    assert clamp_end(start, end, end + timedelta(days=3)) == end.astimezone(UTC)  # past
    assert clamp_end(start, end, start - timedelta(hours=1)) == end.astimezone(UTC)  # future
