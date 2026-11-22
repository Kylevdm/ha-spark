"""Load accuracy scores forecasts against the ledger's derived base load."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.models import ForecastHistoryRecord
from ha_spark.energy.readiness import evaluate_readiness
from ha_spark.energy.readiness_measurements import day_bounds
from ha_spark.energy.stats import compute_stats, load_accuracy
from tests.test_readiness import fixture as readiness_fixture

TZ = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)


def rows_for(day: date, tz: ZoneInfo, *, value=1.0, missing: int | None = None):
    start, end = day_bounds(day, tz.key)
    rows = []
    index = 0
    while start + index * HALF_HOUR < end:
        if index != missing:
            instant = start + index * HALF_HOUR
            rows.append(LedgerSlot(
                slot_start=instant,
                components={"grid_import": ComponentFlow(kwh=value, provenance="boundary")},
                house_load_kwh=value, base_load_kwh=value,
            ))
        index += 1
    return rows, start, end


def forecast(day: date, *, total=20.0, slots=None, median_total=22.0, median_slots=None,
             model="ml", issued=None):
    start, _ = day_bounds(day, TZ.key)
    return ForecastHistoryRecord(
        made_at=issued or start - HALF_HOUR,
        target_date=day,
        model=model,
        total_kwh=total,
        slots=tuple(slots) if slots is not None else None,
        source="test",
        drove_plan=True,
        median_total_kwh=median_total,
        median_slots=tuple(median_slots) if median_slots is not None else None,
    )


def test_per_day_and_shape_errors_for_active_and_median():
    day = date(2026, 6, 1)
    rows, start, end = rows_for(day, TZ)
    result = compute_stats(rows, start, end, TZ)
    active = forecast(day, total=20.0, slots=(2.0,) * 48,
                      median_total=22.0, median_slots=(0.5,) * 48)
    # An earlier pre-day revision and a later in-day revision must not win.
    older = forecast(day, total=99.0, issued=start - timedelta(days=1))
    during_day = forecast(day, total=0.0, issued=start + HALF_HOUR)

    accuracy = load_accuracy(result, [older, active, during_day], TZ)

    assert len(accuracy.days) == 1
    scored = accuracy.days[0]
    assert (scored.day, scored.model) == (day, "ml")
    assert scored.actual_kwh == 48
    assert scored.error_kwh == 28
    assert scored.median_error_kwh == 26
    assert scored.covered_slots == scored.total_slots == 48
    assert scored.complete

    by_model = {entry.model: entry for entry in accuracy.models}
    assert by_model["ml"].days == 1
    assert by_model["ml"].daily_mae_kwh == 28
    assert by_model["ml"].shape_slots == 48
    assert by_model["ml"].shape_mae_kwh == pytest.approx(1.0)
    assert by_model["median"].daily_mae_kwh == 26
    assert by_model["median"].shape_mae_kwh == pytest.approx(0.5)


def test_partial_day_has_no_daily_score_but_scores_covered_shape():
    day = date(2026, 6, 2)
    rows, start, end = rows_for(day, TZ, missing=7)
    result = compute_stats(rows, start, end, TZ)
    accuracy = load_accuracy(
        result,
        [forecast(day, slots=(2.0,) * 48, median_slots=(1.0,) * 48)],
        TZ,
    )

    scored = accuracy.days[0]
    assert scored.actual_kwh is None
    assert scored.error_kwh is None
    assert scored.median_error_kwh is None
    assert scored.covered_slots == 47 and scored.total_slots == 48
    assert scored.coverage == pytest.approx(47 / 48)
    by_model = {entry.model: entry for entry in accuracy.models}
    assert by_model["ml"].days == 0 and by_model["ml"].daily_mae_kwh is None
    assert by_model["ml"].shape_slots == 47


@pytest.mark.parametrize(("day", "count"), [
    (date(2026, 3, 29), 46),
    (date(2026, 10, 25), 50),
])
def test_dst_day_scores_its_real_slots_with_local_slot_index(day, count):
    rows, start, end = rows_for(day, TZ, value=1.0)
    result = compute_stats(rows, start, end, TZ)
    # Predicted slot-of-day value is its index / 100, so DST-day mapping is visible.
    slots = tuple(index / 100 for index in range(48))
    accuracy = load_accuracy(
        result,
        [forecast(day, slots=slots, median_slots=(0.0,) * 48)],
        TZ,
    )

    assert accuracy.days[0].covered_slots == accuracy.days[0].total_slots == count
    by_model = {entry.model: entry for entry in accuracy.models}
    assert by_model["ml"].shape_slots == count
    assert by_model["median"].shape_slots == count


def test_daily_error_matches_readiness_for_same_day_and_model():
    day, forecast_row, guards = readiness_fixture()
    start, end = day_bounds(day.local_date, day.timezone)
    rows = [
        LedgerSlot(
            slot_start=slot.slot_start,
            components={"grid_import": ComponentFlow(kwh=slot.load_kwh, provenance="boundary")},
            house_load_kwh=slot.load_kwh,
            base_load_kwh=slot.load_kwh - slot.components["v2l"].kwh,
        )
        for slot in day.slots
    ]
    stats_result = compute_stats(rows, start, end, TZ)
    accuracy = load_accuracy(stats_result, [forecast_row], TZ)
    readiness = evaluate_readiness([day], guards, [forecast_row], Settings(readiness_window_days=1))

    assert accuracy.days[0].error_kwh == pytest.approx(readiness.model_mae)
    assert accuracy.days[0].median_error_kwh == pytest.approx(readiness.median_mae)


def test_missing_or_future_forecast_is_not_scored():
    day = date(2026, 6, 3)
    rows, start, end = rows_for(day, TZ)
    result = compute_stats(rows, start, end, TZ)

    assert load_accuracy(result, [], TZ).days == ()
    future = forecast(day, issued=datetime.combine(day, time(12), TZ))
    assert load_accuracy(result, [future], TZ).days == ()


def test_current_day_load_coverage_counts_only_the_elapsed_slots():
    day = date(2026, 6, 1)
    rows, start, _ = rows_for(day, TZ)
    now = start + 10 * HALF_HOUR  # The range is clamped to now, as on every surface.
    accuracy = load_accuracy(compute_stats(rows, start, now, TZ), [forecast(day)], TZ)
    [scored] = accuracy.days
    assert scored.covered_slots == scored.total_slots == 10
    assert scored.actual_kwh is scored.error_kwh is None  # A daily error needs the whole day.
