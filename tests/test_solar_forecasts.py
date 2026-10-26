"""The recorded solar forecast: what each plan was issued with, per slot (#505).

`SolarForecastStore` owns its own table, apart from `forecasts` on purpose:
readiness reads `forecasts` directly and picks any `drove_plan` row, and the
ledger readers return every model, so a solar row must never surface as a
load-forecast revision.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import scheduler, sources
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.readiness import load_readiness
from ha_spark.energy.readiness_ledger import ReadinessLedger
from ha_spark.energy.readiness_measurements import ReadinessDays
from ha_spark.energy.scheduler import run_once
from ha_spark.energy.solar_forecasts import SolarForecastStore, recorded_solar
from tests.authority_helpers import legacy_settings
from tests.test_readiness import fixture as readiness_fixture

BASE = "http://ha.test/api"
LONDON = ZoneInfo("Europe/London")
SLOT = datetime(2026, 6, 8, 17, 0, tzinfo=UTC)


def _frozen_clock(frozen: datetime) -> type[datetime]:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _Clock:
            at = frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)
            return cls.fromtimestamp(at.timestamp(), at.tzinfo)

    return _Clock


async def _run_plan(
    s: Settings, monkeypatch: pytest.MonkeyPatch
) -> tuple[scheduler.RunResult, tuple[datetime, ...]]:
    """One ``run_once`` with a slot forecast and today's solar entity mocked."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=12.0, slots=(0.25,) * 48, source="slot profile (14d hourly house stats)",
            target_date=date(2026, 1, 12),
        )

    frozen = datetime(2026, 1, 12, 1, 0, tzinfo=LONDON)
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(frozen))
    monkeypatch.setattr(scheduler, "datetime", _frozen_clock(frozen))
    respx.get(f"{BASE}/states/sensor.solar_today").mock(return_value=httpx.Response(200, json={
        "entity_id": "sensor.solar_today", "state": "2.0",
        "attributes": {
            "estimate": 2.0,
            "detailedForecast": [
                {"period_start": "2026-01-12T02:00:00+00:00", "pv_estimate": 1.0},
            ],
        },
    }))
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    result = await run_once(s)
    geometry = result.plan_run.run.inputs.geometry
    assert geometry is not None
    return result, geometry.slot_starts


@respx.mock
async def test_run_once_records_the_solar_forecast_it_planned_with(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """One row per slot that carried a forecast, stamped with the input snapshot.

    Frozen at 01:00 inside Monday night's window, the horizon spans Monday
    (today's entity, valid, its detailed forecast listing only 02:00) and
    Tuesday (no valid reading, so nothing is stored). Slot 0's own row is issued exactly at its
    start, so it never reads back: only a later plan's earlier issue does.
    """
    s = legacy_settings(
        ha_url="http://ha.test",
        ha_token="t",
        solar_today_entity="sensor.solar_today",
        solar_tomorrow_entity="sensor.solar",
        db_path=str(tmp_path / "ledger.db"),
    )
    result, starts = await _run_plan(s, monkeypatch)

    values = await recorded_solar(s.db_path, starts)
    for start in starts:
        if start == datetime(2026, 1, 12, 1, 0, tzinfo=UTC):
            assert start not in values  # issued exactly at the slot's start
        elif start == datetime(2026, 1, 12, 2, 0, tzinfo=UTC):
            assert values[start] == 2.0
        else:
            # Monday's other slots weren't in the detailed forecast, and Tuesday
            # had no valid reading: neither is stored as a zero.
            assert start not in values
    async with aiosqlite.connect(s.db_path) as db:
        issues = {row[0] for row in await db.execute_fetchall(
            "SELECT DISTINCT issued_at FROM solar_forecasts"
        )}
    assert issues == {"2026-01-12T01:00:00+00:00"}


@respx.mock
async def test_run_once_records_nothing_without_solar_provenance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A run whose solar entities are all unreadable stores no rows at all."""
    s = legacy_settings(
        ha_url="http://ha.test",
        ha_token="t",
        solar_today_entity="sensor.solar_today",
        solar_tomorrow_entity="sensor.solar",
        db_path=str(tmp_path / "ledger.db"),
    )

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(
            total_kwh=12.0, slots=(0.25,) * 48, source="slot profile (14d hourly house stats)",
            target_date=date(2026, 1, 12),
        )

    frozen = datetime(2026, 1, 12, 1, 0, tzinfo=LONDON)
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(frozen))
    monkeypatch.setattr(scheduler, "datetime", _frozen_clock(frozen))
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    await run_once(s)
    async with aiosqlite.connect(s.db_path) as db:
        tables = {row[0] for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
    assert "solar_forecasts" not in tables


@respx.mock
async def test_solar_recording_failure_never_breaks_the_plan_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    class _Broken:
        def __init__(self, _path: str) -> None:
            pass

        async def __aenter__(self) -> _Broken:
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(scheduler, "SolarForecastStore", _Broken)
    s = legacy_settings(
        ha_url="http://ha.test",
        ha_token="t",
        solar_today_entity="sensor.solar_today",
        solar_tomorrow_entity="sensor.solar",
        db_path=str(tmp_path / "ledger.db"),
    )
    with caplog.at_level("WARNING"):
        result, _ = await _run_plan(s, monkeypatch)
    assert result.plan.charge_intent is not None
    failures = [r for r in caplog.records if "Recording solar forecast failed" in r.message]
    # Fixed text only: no exception text or traceback reaches the log.
    assert failures and all(r.exc_info is None for r in failures)


async def test_readiness_and_ledger_readers_ignore_solar_forecasts(tmp_path):
    """Populating `solar_forecasts` changes no readiness or load-forecast read.

    The solar table is separate on purpose: readiness reads `forecasts`
    directly and picks any `drove_plan` row, and the ledger readers return
    every model, so a solar row must never surface as a load-forecast
    revision (#496).
    """
    settings = Settings(db_path=str(tmp_path / "ledger.db"), readiness_window_days=1)
    day, forecast, guards = readiness_fixture()
    await ReadinessDays(settings.db_path).record(day)
    for guard in guards:
        await ReadinessLedger(settings.db_path).record(guard)
    async with ForecastLedger(settings.db_path) as ledger:
        await ledger.record_forecast(
            forecast.made_at, forecast.target_date, forecast.model, forecast.total_kwh,
            None, "test", drove_plan=True, median_total_kwh=forecast.median_total_kwh,
        )
    readiness_before = await load_readiness(settings)
    async with ForecastLedger(settings.db_path) as ledger:
        history_before = await ledger.forecast_history(date(2000, 1, 1))
        since_before = await ledger.forecasts_since(date(2000, 1, 1))

    slot = day.slots[0].slot_start
    async with SolarForecastStore(settings.db_path) as store:
        await store.record(
            slot - timedelta(days=1),
            [(slot, 0.5), (slot + timedelta(minutes=30), 0.25)],
        )

    assert await load_readiness(settings) == readiness_before
    async with ForecastLedger(settings.db_path) as ledger:
        assert await ledger.forecast_history(date(2000, 1, 1)) == history_before
        assert await ledger.forecasts_since(date(2000, 1, 1)) == since_before


async def test_latest_issue_strictly_before_slot_start_wins(tmp_path):
    path = str(tmp_path / "solar.db")
    async with SolarForecastStore(path) as store:
        await store.record(datetime(2026, 6, 8, 12, 0, tzinfo=UTC), [(SLOT, 1.0)])
        await store.record(datetime(2026, 6, 8, 16, 59, tzinfo=UTC), [(SLOT, 2.0)])
        # An issue exactly at the slot's start does not count: the slot had begun.
        await store.record(SLOT, [(SLOT, 9.0)])
    values = await recorded_solar(path, [SLOT])
    assert values == {SLOT: 2.0}


async def test_equal_issue_timestamps_break_by_insertion_order(tmp_path):
    path = str(tmp_path / "solar.db")
    issued = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    async with SolarForecastStore(path) as store:
        await store.record(issued, [(SLOT, 1.0)])
        await store.record(issued, [(SLOT, 2.0)])
    values = await recorded_solar(path, [SLOT])
    assert values == {SLOT: 2.0}


async def test_slots_without_a_forecast_read_as_null(tmp_path):
    path = str(tmp_path / "solar.db")
    covered = datetime(2026, 6, 8, 12, 30, tzinfo=UTC)
    async with SolarForecastStore(path) as store:
        await store.record(datetime(2026, 6, 8, 12, 0, tzinfo=UTC), [(covered, 0.5)])
    values = await recorded_solar(path, [covered, SLOT])
    # The uncovered slot is absent, never zero; a real zero stays zero.
    assert values == {covered: 0.5}


async def test_recorded_solar_never_creates_the_database(tmp_path):
    path = str(tmp_path / "absent.db")
    assert await recorded_solar(path, [SLOT]) == {}
    assert not (tmp_path / "absent.db").exists()


async def test_rows_more_than_seven_days_old_are_pruned_on_write(tmp_path):
    path = str(tmp_path / "solar.db")
    now = datetime(2026, 6, 8, 13, 0, tzinfo=UTC)
    kept_boundary = now - timedelta(days=7)  # exactly 7 days old: kept
    pruned = now - timedelta(days=7, minutes=30)
    async with SolarForecastStore(path) as store:
        await store.record(now, [(pruned, 1.0), (kept_boundary, 2.0), (SLOT, 3.0)])
    async with aiosqlite.connect(path) as db:
        rows = await db.execute_fetchall("SELECT slot_start FROM solar_forecasts")
    assert sorted(row[0] for row in rows) == [
        kept_boundary.astimezone(UTC).isoformat(), SLOT.astimezone(UTC).isoformat(),
    ]


async def test_read_finds_no_rows_in_a_database_without_the_table(tmp_path):
    path = str(tmp_path / "ledger.db")
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE forecasts (id INTEGER PRIMARY KEY)")
        await db.commit()
    assert await recorded_solar(path, [SLOT]) == {}


@pytest.mark.parametrize("tz_name", ["Europe/London"])
async def test_issued_and_slot_starts_are_stored_in_utc(tmp_path, tz_name):
    """Household-clock inputs are stored as absolute UTC instants."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    path = str(tmp_path / "solar.db")
    issued = datetime(2026, 6, 8, 12, 0, tzinfo=tz)
    slot = datetime(2026, 6, 8, 17, 0, tzinfo=tz)
    async with SolarForecastStore(path) as store:
        await store.record(issued, [(slot, 1.25)])
    async with aiosqlite.connect(path) as db:
        rows = await db.execute_fetchall("SELECT issued_at, slot_start, kwh FROM solar_forecasts")
    assert rows == [("2026-06-08T11:00:00+00:00", "2026-06-08T16:00:00+00:00", 1.25)]
    # A reader keyed by the same instant in another zone still finds the row.
    values = await recorded_solar(path, [slot.astimezone(UTC)])
    assert values == {slot.astimezone(UTC): 1.25}
