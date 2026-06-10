"""Tests for the grid-import cost backtest."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.backtest import backtest_cost, format_backtest
from ha_spark.energy.models import ConsumptionInterval

UTC_TZ = ZoneInfo("UTC")
WINDOW = {"window_start": time(23, 30), "window_end": time(5, 30)}
RATES = {"rate_offpeak": 0.10, "rate_peak": 0.30}


def _iv(start: datetime, kwh: float) -> ConsumptionInterval:
    return ConsumptionInterval(start, start + timedelta(minutes=30), kwh)


def test_backtest_classifies_window_with_midnight_wrap() -> None:
    intervals = [
        _iv(datetime(2026, 6, 1, 23, 30, tzinfo=UTC), 1.0),  # in window (before midnight)
        _iv(datetime(2026, 6, 2, 3, 0, tzinfo=UTC), 2.0),  # in window (after midnight)
        _iv(datetime(2026, 6, 2, 12, 0, tzinfo=UTC), 4.0),  # peak
    ]
    s = backtest_cost(intervals, tz=UTC_TZ, **WINDOW, **RATES)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(3.0)
    assert s.peak_kwh == pytest.approx(4.0)
    assert s.total_kwh == pytest.approx(7.0)
    assert s.offpeak_cost == pytest.approx(0.30)
    assert s.peak_cost == pytest.approx(1.20)
    assert s.total_cost == pytest.approx(1.50)
    assert s.days == 2
    assert s.first == intervals[0].start
    assert s.last == intervals[-1].start


def test_backtest_uses_local_time_for_classification() -> None:
    # 22:45 UTC is 23:45 in London (BST) — inside the window locally.
    interval = _iv(datetime(2026, 6, 1, 22, 45, tzinfo=UTC), 1.0)
    s = backtest_cost([interval], tz=ZoneInfo("Europe/London"), **WINDOW, **RATES)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(1.0)
    assert s.peak_kwh == 0.0


def test_backtest_empty_returns_none() -> None:
    assert backtest_cost([], tz=UTC_TZ, **WINDOW, **RATES) is None


def test_format_backtest_renders_totals() -> None:
    s = backtest_cost(
        [_iv(datetime(2026, 6, 2, 12, 0, tzinfo=UTC), 4.0)], tz=UTC_TZ, **WINDOW, **RATES
    )
    assert s is not None
    out = format_backtest(s)
    assert "1 days" in out
    assert "£   1.20" in out
    assert "(£1.20/day)" in out
