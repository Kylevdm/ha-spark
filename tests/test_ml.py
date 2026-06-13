"""Tests for the weather-aware quantile ML load model (needs the [habits] extra)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.ml import ml_available, train_and_predict
from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval

pytest.importorskip("sklearn")

_TZ = ZoneInfo("UTC")
_START = datetime(2026, 5, 1, tzinfo=UTC)


def _history(
    days: int, *, base_kwh: float = 0.4, cold_extra: float = 0.4
) -> tuple[list[ConsumptionInterval], dict[datetime, float]]:
    """Synthetic history: alternating warm (18°C) and cold (5°C) days; the cold
    days carry ``cold_extra`` more load per slot (a heating signature)."""
    intervals: list[ConsumptionInterval] = []
    temps: dict[datetime, float] = {}
    for d in range(days):
        day_start = _START + timedelta(days=d)
        cold = d % 2 == 1
        temp = 5.0 if cold else 18.0
        for h in range(24):
            temps[day_start + timedelta(hours=h)] = temp
        for s in range(SLOTS_PER_DAY):
            start = day_start + timedelta(minutes=30 * s)
            kwh = base_kwh + (cold_extra if cold else 0.0)
            intervals.append(ConsumptionInterval(start, start + timedelta(minutes=30), kwh))
    return intervals, temps


def _add_target_temps(temps: dict[datetime, float], target: date, temp: float) -> None:
    day_start = datetime(target.year, target.month, target.day, tzinfo=UTC)
    for h in range(24):
        temps[day_start + timedelta(hours=h)] = temp


def test_ml_available_with_extra_installed() -> None:
    assert ml_available() is True


def test_prediction_shape_and_quantile_ordering() -> None:
    intervals, temps = _history(30)
    target = (_START + timedelta(days=30)).date()
    _add_target_temps(temps, target, 10.0)
    pred = train_and_predict(intervals, temps, {}, target, _TZ)
    assert pred is not None
    assert len(pred.p50) == SLOTS_PER_DAY and len(pred.p90) == SLOTS_PER_DAY
    assert all(v >= 0 for v in pred.p50)
    assert all(hi >= lo for hi, lo in zip(pred.p90, pred.p50, strict=True))
    assert pred.days_used == 29


def test_cold_forecast_predicts_more_load_than_warm() -> None:
    intervals, temps = _history(30)
    target = (_START + timedelta(days=30)).date()

    cold_temps = dict(temps)
    _add_target_temps(cold_temps, target, 2.0)
    cold = train_and_predict(intervals, cold_temps, {}, target, _TZ)

    warm_temps = dict(temps)
    _add_target_temps(warm_temps, target, 20.0)
    warm = train_and_predict(intervals, warm_temps, {}, target, _TZ)

    assert cold is not None and warm is not None
    assert sum(cold.p50) > sum(warm.p50)


def test_returns_none_when_history_too_thin() -> None:
    intervals, temps = _history(5)
    target = (_START + timedelta(days=5)).date()
    _add_target_temps(temps, target, 10.0)
    assert train_and_predict(intervals, temps, {}, target, _TZ, min_days=14) is None


def test_returns_none_without_target_temperatures() -> None:
    intervals, temps = _history(30)
    target = (_START + timedelta(days=30)).date()  # no temps added for the target
    assert train_and_predict(intervals, temps, {}, target, _TZ) is None


def test_returns_none_without_any_temperatures() -> None:
    intervals, _ = _history(30)
    target = (_START + timedelta(days=30)).date()
    assert train_and_predict(intervals, {}, {}, target, _TZ) is None
