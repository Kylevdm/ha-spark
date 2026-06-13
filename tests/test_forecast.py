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


# --- Phase 6B: weather-aware ML model selection / gating ---


def test_forecast_model_tag_mapping() -> None:
    from ha_spark.energy.forecast import forecast_model_tag

    assert forecast_model_tag("ml quantile gbr (20d, weather-aware)") == "ml"
    assert forecast_model_tag("slot profile (14d hourly house stats)") == "slots"
    assert forecast_model_tag("median of 7d house consumption (stats)") == "median"
    assert forecast_model_tag("configured baseline (stats unavailable)") == "baseline"


def _ml_settings(tmp_path: Any, **overrides: Any) -> Settings:
    return Settings(
        ha_url="http://ha.test",
        ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        **overrides,
    )


def _patch_ml(
    monkeypatch: pytest.MonkeyPatch, prediction: Any
) -> None:
    """Stub the weather fetch and the model so no network/sklearn is needed."""
    from ha_spark.energy import ml, weather

    async def fake_temps(*args: Any, **kwargs: Any) -> dict[Any, Any]:
        return {}

    def fake_train(*args: Any, **kwargs: Any) -> Any:
        return prediction

    monkeypatch.setattr(weather, "hourly_temps", fake_temps)
    monkeypatch.setattr(ml, "ml_available", lambda: True)
    monkeypatch.setattr(ml, "train_and_predict", fake_train)


def _stub_prediction(p50_each: float = 0.4, p90_each: float = 0.5) -> Any:
    from ha_spark.energy.ml import MLPrediction

    return MLPrediction(
        p50=(p50_each,) * SLOTS_PER_DAY, p90=(p90_each,) * SLOTS_PER_DAY, days_used=20
    )


async def test_load_model_ml_prefers_ml_and_records_shadows(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_spark.energy.ledger import ForecastLedger

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    _patch_ml(monkeypatch, _stub_prediction())

    settings = _ml_settings(tmp_path, load_model="ml")
    result = await predict_home_load(settings, lat=51.5, lon=-0.1)

    assert result.source.startswith("ml")
    assert result.total_kwh == pytest.approx(0.4 * SLOTS_PER_DAY)
    assert result.p90_total_kwh == pytest.approx(0.5 * SLOTS_PER_DAY)

    # Both the ML and the median forecast were shadow-recorded for eval.
    from datetime import date as date_cls

    async with ForecastLedger(settings.db_path) as ledger:
        rows = await ledger.forecasts_since(date_cls.min)
    assert {r.model for r in rows} == {"ml", "slots"}


async def test_load_model_median_never_runs_ml(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_spark.energy import ml

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ML must not run with load_model=median")

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    monkeypatch.setattr(ml, "train_and_predict", boom)

    settings = _ml_settings(tmp_path, load_model="median")
    result = await predict_home_load(settings, lat=51.5, lon=-0.1)
    assert "slot profile" in result.source


async def test_load_model_auto_stays_on_median_without_eval_history(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    _patch_ml(monkeypatch, _stub_prediction())

    settings = _ml_settings(tmp_path, load_model="auto")
    result = await predict_home_load(settings, lat=51.5, lon=-0.1)
    assert "slot profile" in result.source  # ML computed but not yet trusted


async def test_load_model_auto_uses_ml_once_it_beats_the_median(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_spark.energy.ledger import ForecastLedger

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)  # actual = 24 kWh/day

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    _patch_ml(monkeypatch, _stub_prediction())

    settings = _ml_settings(tmp_path, load_model="auto")
    # Seed 8 scored days where ML (24.5) was far closer to the 24.0 actual
    # than the median path (30.0).
    today = datetime.now(UTC).date()
    async with ForecastLedger(settings.db_path) as ledger:
        for back in range(1, 9):
            target = today - timedelta(days=back)
            made = datetime.now(UTC) - timedelta(days=back + 1)
            await ledger.record_forecast(made, target, "ml", 24.5, None, "ml quantile gbr")
            await ledger.record_forecast(made, target, "slots", 30.0, None, "slot profile")

    result = await predict_home_load(settings, lat=51.5, lon=-0.1)
    assert result.source.startswith("ml")


async def test_no_location_skips_ml(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_spark.energy import ml

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ML must not run without coordinates")

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    monkeypatch.setattr(ml, "train_and_predict", boom)

    settings = _ml_settings(tmp_path, load_model="ml")
    result = await predict_home_load(settings)  # no lat/lon
    assert "slot profile" in result.source


# --- Phase 6C: context scaling of the forecast ---


async def test_context_away_scales_forecast_down(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from ha_spark.energy.context import ContextStore
    from ha_spark.energy.forecast import load_timezone

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)  # 24 kWh/day unscaled

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    settings = Settings(db_path=str(tmp_path / "ctx.db"), load_model="median",
                        away_load_factor=0.4)
    tz = load_timezone(settings.timezone)
    tomorrow = (_dt.now(tz) + _td(days=1)).date()
    async with ContextStore(settings.db_path) as store:
        await store.add("away", tomorrow, tomorrow, note="holiday")

    result = await predict_home_load(settings)
    assert result.total_kwh == pytest.approx(24.0 * 0.4)
    assert "context" in result.source and "away" in result.source
    assert result.slots is not None
    assert all(s == pytest.approx(0.5 * 0.4) for s in result.slots)


async def test_no_active_context_leaves_forecast_unscaled(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return _hourly_rows(days=14, kwh_per_hour=1.0)

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    settings = Settings(db_path=str(tmp_path / "ctx.db"), load_model="median")
    result = await predict_home_load(settings)
    assert result.total_kwh == pytest.approx(24.0)
    assert "context" not in result.source


# --- Phase 6E: learned away factor overrides the configured default ---


async def test_learned_away_factor_overrides_config(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime as _dt

    from ha_spark.energy.context import ContextStore
    from ha_spark.energy.forecast import load_timezone

    tz = load_timezone("UTC")
    today = _dt.now(tz).date()
    tomorrow = today + timedelta(days=1)
    # Three past away days (well before today) drew 40% of a normal 24 kWh day.
    away_past = [today - timedelta(days=d) for d in (10, 9, 8)]

    def rows_for(_days: int, _kwh: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        today_mid = _dt.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        for d in range(21, 0, -1):
            day = today_mid - timedelta(days=d)
            per_hour = 0.4 if day.date() in away_past else 1.0
            out.extend(
                {"start": (day + timedelta(hours=h)).timestamp() * 1000, "change": per_hour}
                for h in range(24)
            )
        return out

    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return rows_for(21, 1.0)

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    settings = Settings(
        db_path=str(tmp_path / "ctx.db"), load_model="median", away_load_factor=0.7
    )
    async with ContextStore(settings.db_path) as store:
        # Record the past away block (for learning) and tomorrow (active).
        await store.add("away", away_past[0], away_past[-1])
        await store.add("away", tomorrow, tomorrow, note="holiday")

    result = await predict_home_load(settings)
    # Learned factor ~0.4 (away/normal), not the configured 0.7.
    assert result.total_kwh == pytest.approx(24.0 * 0.4, abs=0.5)
    assert "learned" in result.source
