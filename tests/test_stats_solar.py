"""Solar accuracy from forecast and measured slot evidence, with no I/O."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.evening_forecast import SolarForecastSlot
from ha_spark.energy.readiness_measurements import day_bounds
from ha_spark.energy.stats import compute_stats, solar_accuracy

TZ = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)


def test_solar_errors_and_band_hits_use_only_comparable_slots():
    start, _ = day_bounds(date(2026, 1, 2), TZ.key)
    starts = [start + HALF_HOUR * i for i in range(6)]
    rows = [
        LedgerSlot(slot_start=at, components={"solar_generation": flow})
        for at, flow in zip(starts, (
            ComponentFlow(kwh=3, provenance="boundary"),
            ComponentFlow(kwh=1, provenance="statistics_fill"),
            ComponentFlow(kwh=0, provenance="hourly_backfill"),
            ComponentFlow(kwh=9, provenance="absent"),
            ComponentFlow(kwh=8, provenance="boundary"),
        ), strict=False)
    ]
    forecast = [
        SolarForecastSlot(start=at, p50_kwh=p50, p10_kwh=p10, p90_kwh=p90)
        for at, (p50, p10, p90) in zip(starts, (
            (2, 1, 3), (4, 2, 5), (0, None, None), (1, 0, 2), (None, None, None), (2, 1, 3),
        ), strict=True)
    ]
    result = solar_accuracy(compute_stats(rows, start, starts[-1] + HALF_HOUR, TZ), forecast)

    assert [slot.error_kwh for slot in result.slots] == [-1, 3, 0, None, None, None]
    assert [slot.band_hit for slot in result.slots] == [True, False, None, None, None, None]
    assert [slot.measured_kwh for slot in result.slots] == [3, 1, 0, None, 8, None]
    assert result.mae_kwh == pytest.approx(4 / 3)
    assert result.bias_kwh == pytest.approx(2 / 3)
    assert result.scored_slots == 3
    assert result.coverage == 0.5
    assert result.reason is None


def test_no_evening_plan_is_explicit_in_coverage():
    start, end = day_bounds(date(2026, 1, 2), TZ.key)
    result = solar_accuracy(compute_stats([], start, end, TZ), [])
    assert result.scored_slots == 0
    assert result.coverage == 0
    assert result.reason == "no evening plan"
    assert result.mae_kwh is result.bias_kwh is None
    assert all(slot.forecast is None for slot in result.slots)


def test_recorded_plan_without_solar_does_not_imply_zero_forecast():
    start, end = day_bounds(date(2026, 1, 2), TZ.key)
    result = solar_accuracy(compute_stats([], start, end, TZ), [SolarForecastSlot(start=start)])
    assert result.reason == "no comparable solar slots"
    assert result.coverage == 0
    assert result.mae_kwh is result.bias_kwh is None


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 46), (date(2026, 10, 25), 50)])
def test_solar_coverage_counts_real_dst_slots(day, count):
    start, end = day_bounds(day, TZ.key)
    starts = [start + i * HALF_HOUR for i in range(count)]
    forecast = [SolarForecastSlot(start=at, p50_kwh=i) for i, at in enumerate(starts)]
    rows = [
        LedgerSlot(slot_start=at, components={
            "solar_generation": ComponentFlow(kwh=i - 1, provenance="boundary"),
        })
        for i, at in enumerate(starts) if i
    ]
    result = solar_accuracy(compute_stats(rows, start, end, TZ), forecast)
    assert len(result.slots) == count
    assert result.coverage == (count - 1) / count
    assert [slot.error_kwh for slot in result.slots] == [None] + [1] * (count - 1)
    assert result.mae_kwh == result.bias_kwh == 1


def test_non_finite_range_error_is_unknown():
    start, _ = day_bounds(date(2026, 1, 2), TZ.key)
    rows = [
        LedgerSlot(slot_start=at, components={
            "solar_generation": ComponentFlow(kwh=0, provenance="boundary"),
        })
        for at in (start, start + HALF_HOUR)
    ]
    forecasts = [SolarForecastSlot(start=row.slot_start, p50_kwh=1e308) for row in rows]
    result = solar_accuracy(compute_stats(rows, start, start + 2 * HALF_HOUR, TZ), forecasts)
    assert result.coverage == 1
    assert result.mae_kwh is result.bias_kwh is None
