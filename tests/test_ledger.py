"""Tests for the forecast/signal ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from ha_spark.energy.ledger import ForecastLedger


async def test_record_forecast_roundtrip(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    made_at = datetime(2026, 6, 12, 22, 0, tzinfo=UTC)
    target = date(2026, 6, 13)
    async with ForecastLedger(db) as ledger:
        await ledger.record_forecast(made_at, target, "slots", 24.5, [1.0, 2.0], "slot profile")
        rows = await ledger.forecasts_since(target)
    assert len(rows) == 1
    row = rows[0]
    assert row.target_date == target
    assert row.model == "slots"
    assert row.total_kwh == 24.5
    assert row.slots == (1.0, 2.0)
    assert row.source == "slot profile"


async def test_record_forecast_upserts_same_target_and_model(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    target = date(2026, 6, 13)
    async with ForecastLedger(db) as ledger:
        await ledger.record_forecast(
            datetime(2026, 6, 12, 22, 0, tzinfo=UTC), target, "median", 20.0, None, "median of 7d"
        )
        await ledger.record_forecast(
            datetime(2026, 6, 12, 22, 30, tzinfo=UTC), target, "median", 21.0, None, "median of 8d"
        )
        rows = await ledger.forecasts_since(target)
    assert len(rows) == 1
    assert rows[0].total_kwh == 21.0
    assert rows[0].source == "median of 8d"


async def test_forecasts_since_filters_by_date(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    async with ForecastLedger(db) as ledger:
        await ledger.record_forecast(
            datetime(2026, 6, 1, tzinfo=UTC), date(2026, 6, 2), "median", 10.0, None, "x"
        )
        await ledger.record_forecast(
            datetime(2026, 6, 10, tzinfo=UTC), date(2026, 6, 11), "median", 12.0, None, "x"
        )
        rows = await ledger.forecasts_since(date(2026, 6, 5))
    assert [r.target_date for r in rows] == [date(2026, 6, 11)]


async def test_record_signal_roundtrip(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    ts = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    async with ForecastLedger(db) as ledger:
        await ledger.record_signal(ts, "occupancy_home_frac", 0.5)
        since = datetime(2026, 1, 1, tzinfo=UTC)
        history = await ledger.signal_history("occupancy_home_frac", since)
    assert history == [(ts, 0.5)]


async def test_record_signal_upserts(tmp_path: Path) -> None:
    db = str(tmp_path / "test.db")
    ts = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    async with ForecastLedger(db) as ledger:
        await ledger.record_signal(ts, "temp_out_c", 10.0)
        await ledger.record_signal(ts, "temp_out_c", 12.0)
        history = await ledger.signal_history("temp_out_c", datetime(2026, 1, 1, tzinfo=UTC))
    assert history == [(ts, 12.0)]
