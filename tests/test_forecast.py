"""Tests for the home-load forecast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ha_spark.config import Settings
from ha_spark.energy import forecast
from ha_spark.energy.forecast import _daily_totals, predict_home_load, predict_home_load_kwh
from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval
from ha_spark.energy.store import ConsumptionStore


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


# --- v2 fallback chain (slot profile -> HA stats -> baseline) ---


async def _seed_history(db_path: str, days: int, kwh: float) -> None:
    """Write `days` full days of half-hourly readings ending yesterday (UTC)."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for d in range(1, days + 1):
        day_start = today - timedelta(days=d)
        rows.extend(
            ConsumptionInterval(
                start=day_start + timedelta(minutes=30 * i),
                end=day_start + timedelta(minutes=30 * (i + 1)),
                kwh=kwh,
            )
            for i in range(SLOTS_PER_DAY)
        )
    async with ConsumptionStore(db_path) as store:
        await store.upsert(rows, "csv")


async def test_predict_home_load_uses_slot_profile(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    await _seed_history(db, days=14, kwh=0.5)
    result = await predict_home_load(Settings(db_path=db))
    assert result.slots is not None
    assert len(result.slots) == SLOTS_PER_DAY
    assert result.total_kwh == pytest.approx(24.0)
    assert "slot profile" in result.source


async def test_predict_home_load_falls_back_to_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_stats(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"change": 20.0}, {"change": 24.0}, {"change": 28.0}]

    monkeypatch.setattr(forecast, "statistics_during_period", fake_stats)
    result = await predict_home_load(Settings(db_path=str(tmp_path / "empty.db")))
    assert result.slots is None
    assert result.total_kwh == 24.0
    assert "stats" in result.source


async def test_predict_home_load_falls_back_to_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("ws down")

    monkeypatch.setattr(forecast, "statistics_during_period", boom)
    result = await predict_home_load(
        Settings(db_path=str(tmp_path / "empty.db"), expected_load_kwh=21.0)
    )
    assert result.slots is None
    assert result.total_kwh == 21.0
    assert "baseline" in result.source
