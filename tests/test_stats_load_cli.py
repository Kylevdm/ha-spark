"""The stats CLI renders load accuracy; the arithmetic stays in the pure function."""

import asyncio
import sqlite3
from datetime import date, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.readiness_measurements import day_bounds

TZ = ZoneInfo("Europe/London")
DAY = date(2026, 6, 1)
HALF_HOUR = timedelta(minutes=30)


def _rows():
    start, end = day_bounds(DAY, TZ.key)
    rows = []
    instant = start
    while instant < end:
        rows.append(LedgerSlot(
            slot_start=instant,
            components={"grid_import": ComponentFlow(kwh=1.0, provenance="boundary")},
            house_load_kwh=1.0, base_load_kwh=1.0,
        ))
        instant += HALF_HOUR
    return rows, start


def test_stats_cli_prints_load_accuracy(monkeypatch, capsys, tmp_path):
    settings = Settings(timezone=TZ.key, db_path=str(tmp_path / "stats.db"))
    _, start = _rows()

    async def seed():
        async with ForecastLedger(settings.db_path) as ledger:
            await ledger.record_forecast(
                start - timedelta(hours=1), DAY, "ml", 20.0, (2.0,) * 48, "test",
                drove_plan=True, median_total_kwh=22.0, median_slots=(0.5,) * 48,
            )

    asyncio.run(seed())
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=_rows()[0]))

    assert cli.main(["stats", "--range", "day", "--at", DAY.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "Load accuracy (vs derived base load):" in out
    assert "daily error" in out and "ml" in out
    assert "median" in out
    assert "shape MAE" in out


def test_stats_cli_without_forecasts_says_so(monkeypatch, capsys, tmp_path):
    settings = Settings(timezone=TZ.key, db_path=str(tmp_path / "stats.db"))
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=_rows()[0]))

    assert cli.main(["stats", "--range", "day", "--at", DAY.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "Load accuracy (vs derived base load):\n  no recorded forecasts\n" in out


def test_forecast_history_reader_is_read_only_and_tolerates_missing(tmp_path):
    missing = tmp_path / "missing.db"
    rows = asyncio.run(cli._read_forecast_history(str(missing), DAY, DAY))
    assert rows == []
    assert not missing.exists()


def test_forecast_history_reader_skips_malformed_rows(tmp_path):
    settings = Settings(timezone=TZ.key, db_path=str(tmp_path / "stats.db"))
    _, start = _rows()

    async def seed():
        async with ForecastLedger(settings.db_path) as ledger:
            await ledger.record_forecast(
                start - timedelta(hours=1), DAY, "ml", 20.0, (2.0,) * 48, "test",
                drove_plan=True, median_total_kwh=22.0, median_slots=(0.5,) * 48,
            )

    asyncio.run(seed())
    with sqlite3.connect(settings.db_path) as conn:
        good = conn.execute("SELECT * FROM forecasts").fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(forecasts)")]
        for change in (
            {"total_kwh": "x"},
            {"total_kwh": float("inf")},
            {"issued_at": "2026-05-31T22:00:00"},
            {"issued_at": "0001-01-01T00:00:00+01:00"},
        ):
            row = dict(zip(columns, good, strict=True)) | change | {"id": None}
            conn.execute(
                f"INSERT INTO forecasts ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )

    rows = asyncio.run(cli._read_forecast_history(settings.db_path, DAY, DAY))
    assert len(rows) == 1
    assert rows[0].slots == (2.0,) * 48


def test_forecast_history_reader_drops_only_a_malformed_slot_series(tmp_path):
    settings = Settings(timezone=TZ.key, db_path=str(tmp_path / "stats.db"))
    _, start = _rows()

    async def seed():
        async with ForecastLedger(settings.db_path) as ledger:
            await ledger.record_forecast(
                start - timedelta(hours=1), DAY, "ml", 20.0, (2.0,) * 48, "test",
                drove_plan=True, median_total_kwh=22.0, median_slots=(0.5,) * 48,
            )

    asyncio.run(seed())
    for slots, median in (('{"1": 99}', "[true]"), ('["x"]', "[NaN]"), ("[1e999999]", "")):
        with sqlite3.connect(settings.db_path) as conn:
            conn.execute(
                "UPDATE forecasts SET slots_json = ?, median_slots_json = ?", (slots, median),
            )
        rows = asyncio.run(cli._read_forecast_history(settings.db_path, DAY, DAY))
        assert [(r.total_kwh, r.slots, r.median_slots) for r in rows] == [(20.0, None, None)]
