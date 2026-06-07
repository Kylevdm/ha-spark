"""Tests for the home-load forecast."""

from __future__ import annotations

from typing import Any

import pytest

from ha_spark.config import Settings
from ha_spark.energy import forecast
from ha_spark.energy.forecast import _daily_totals, predict_home_load_kwh


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
