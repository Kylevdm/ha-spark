"""Tests for the home-load forecast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ha_spark.config import Settings
from ha_spark.energy import forecast
from ha_spark.energy.forecast import _daily_totals, predict_home_load, predict_home_load_kwh
from ha_spark.energy.models import SLOTS_PER_DAY


def test_daily_totals_prefers_change() -> None:
    rows = [{"change": 10.0}, {"change": 12.0}, {"change": -1.0}]
    assert _daily_totals(rows) == [10.0, 12.0]


def test_daily_totals_diffs_cumulative_sum() -> None:
    rows = [{"sum": 100.0}, {"sum": 110.0}, {"sum": 125.0}]
    assert _daily_totals(rows) == [10.0, 15.0]


async def test_predict_uses_median(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"change": 20.0}, {"change": 24.0}, {"change": 28.0}]

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    value, source = await predict_home_load_kwh(Settings())
    assert value == 24.0
    assert "stats" in source


async def test_predict_falls_back_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    monkeypatch.setattr(forecast, "statistics_during_period", boom)
    value, source = await predict_home_load_kwh(Settings(expected_load_kwh=21.0))
    assert value == 21.0
    assert "baseline" in source


# --- v2 fallback chain (slot profile from hourly stats -> daily stats -> baseline) ---


def _hourly_rows(days: int, kwh_per_hour: float) -> list[dict[str, Any]]:
    """`days` full days of hourly stats rows ending yesterday (UTC), epoch-ms starts."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for d in range(days, 0, -1):
        day_start = today - timedelta(days=d)
        rows.extend(
            {
                "start": (day_start + timedelta(hours=h)).timestamp() * 1000,
                "change": kwh_per_hour,
            }
            for h in range(24)
        )
    return rows


async def test_predict_home_load_uses_slot_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs.get("period") == "hour"
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    result = await predict_home_load(Settings())
    assert result.slots is not None
    assert len(result.slots) == SLOTS_PER_DAY
    assert result.total_kwh == pytest.approx(24.0)
    assert "slot profile" in result.source
    assert "house stats" in result.source


async def test_predict_home_load_falls_back_to_daily_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("period") == "hour":
            # Too thin for a profile: a single day of hourly history.
            return _hourly_rows(days=1, kwh_per_hour=1.0)
        return [{"change": 20.0}, {"change": 24.0}, {"change": 28.0}]

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    result = await predict_home_load(Settings())
    assert result.slots is None
    assert result.total_kwh == 24.0
    assert "stats" in result.source


async def test_predict_home_load_falls_back_to_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    monkeypatch.setattr(forecast, "statistics_during_period", boom)
    result = await predict_home_load(Settings(expected_load_kwh=21.0))
    assert result.slots is None
    assert result.total_kwh == 21.0
    assert "baseline" in result.source


def testintervals_from_hourly_stats_splits_and_filters() -> None:
    rows = [
        {"start": 1780304400000, "change": 1.0},
        {"start": 1780308000000, "change": None},
        {"start": 1780311600000, "change": -0.5},
        {"start": None, "change": 2.0},
    ]
    intervals = forecast.intervals_from_hourly_stats(rows)
    assert len(intervals) == 2
    assert all(i.kwh == 0.5 for i in intervals)
    assert intervals[0].start == datetime.fromtimestamp(1780304400, UTC)
    assert intervals[1].start - intervals[0].start == timedelta(minutes=30)
    assert intervals[1].end - intervals[1].start == timedelta(minutes=30)
