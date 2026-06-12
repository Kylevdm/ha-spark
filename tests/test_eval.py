"""Tests for forecast-vs-actual accuracy scoring."""

from __future__ import annotations

from datetime import UTC, date, datetime

from ha_spark.energy.eval import actual_kwh_by_date, evaluate, format_eval
from ha_spark.energy.models import ForecastRecord


def _forecast(target: date, model: str, total: float) -> ForecastRecord:
    return ForecastRecord(
        made_at=datetime(2026, 6, 1, tzinfo=UTC),
        target_date=target,
        model=model,
        total_kwh=total,
        slots=None,
        source="test",
    )


def test_actual_kwh_by_date_uses_change() -> None:
    rows = [
        {"start": 1748736000000, "change": 20.0},  # 2025-06-01 00:00 UTC
        {"start": 1748822400000, "change": -1.0},  # negative discarded
    ]
    actuals = actual_kwh_by_date(rows)
    assert actuals == {date(2025, 6, 1): 20.0}


def test_evaluate_computes_mae_and_mape_per_model() -> None:
    forecasts = [
        _forecast(date(2026, 6, 1), "median", 20.0),
        _forecast(date(2026, 6, 2), "median", 22.0),
        _forecast(date(2026, 6, 1), "slots", 24.0),
        _forecast(date(2026, 6, 2), "slots", 21.0),
    ]
    actuals = {date(2026, 6, 1): 24.0, date(2026, 6, 2): 20.0}
    results = {r.model: r for r in evaluate(forecasts, actuals)}

    assert results["median"].n == 2
    assert results["median"].mae_kwh == 3.0  # (|20-24| + |22-20|) / 2
    assert results["slots"].n == 2
    assert results["slots"].mae_kwh == 0.5  # (|24-24| + |21-20|) / 2


def test_evaluate_skips_unmatched_dates() -> None:
    forecasts = [_forecast(date(2026, 6, 1), "median", 20.0)]
    results = evaluate(forecasts, {date(2026, 6, 2): 10.0})
    assert results == []


def test_format_eval_empty() -> None:
    assert "No recorded forecasts" in format_eval([], 14)


def test_format_eval_lists_models() -> None:
    forecasts = [_forecast(date(2026, 6, 1), "median", 20.0)]
    results = evaluate(forecasts, {date(2026, 6, 1): 22.0})
    out = format_eval(results, 14)
    assert "median" in out
    assert "MAE" in out
    assert "MAPE" in out
