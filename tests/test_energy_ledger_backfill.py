"""Pre-install energy history through fake HA statistics and the public ledger reader."""

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import ledger_backfill, scheduler
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot, RateEvidence
from ha_spark.energy.stats import compute_stats
from tests.authority_helpers import run_daemon, seed_authority
from tests.test_authority_gate import daemon_ticks
from tests.test_energy_ledger_scheduler import stub_control, wait_at_tick_end
from tests.test_readiness_ledger import START as TICK

START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
HALF_HOUR = HOUR / 2


def settings_for(tmp_path, **kwargs):
    return Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ledger.db"),
        timezone="Europe/London", derive_grid_import_entity="sensor.grid_import", **kwargs,
    )


def hour(start, **values):
    return {"start": int(start.timestamp() * 1000), **values}


def fake_recorder(monkeypatch, series):
    async def metadata(*args, **kwargs):
        return [
            {"statistic_id": entity, "statistics_unit_of_measurement": unit}
            for entity, (unit, _) in series.items()
        ]

    async def statistics(url, token, entity, start, *, period, end_time, **kwargs):
        assert period == "hour"
        assert start == end_time - timedelta(days=ledger_backfill.BACKFILL_DAYS)
        return series[entity][1]

    monkeypatch.setattr(ledger_backfill, "list_statistic_ids", metadata)
    monkeypatch.setattr(ledger_backfill, "statistics_during_period", statistics)


async def record_first_slot(settings, start):
    # No configured sensors on this tick: no REST I/O and still real recorder evidence.
    tick_settings = Settings(db_path=settings.db_path, timezone=settings.timezone)
    await EnergyLedger(settings.db_path).record_boundary(
        tick_settings, start + HALF_HOUR, RateEvidence(import_rate=.3, export_rate=.1),
    )


async def test_hourly_backfill_splits_flows_and_counts_toward_energy_coverage(
    tmp_path, monkeypatch,
):
    settings = settings_for(tmp_path, derive_invert_grid_export=True,
                            derive_grid_export_entity="sensor.grid_export",
                            derive_ev_charge_entity="sensor.ev_charge",
                            v2l_power_entity="sensor.v2l_power")
    await record_first_slot(settings, START + HOUR)
    fake_recorder(monkeypatch, {
        "sensor.grid_import": ("Wh", [hour(START, change="8000")]),
        "sensor.grid_export": ("kWh", [hour(START, change=-2)]),
        "sensor.ev_charge": ("kWh", [hour(START, change=1)]),
        "sensor.v2l_power": ("W", [hour(START, mean=2000)]),
    })
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await EnergyLedger(settings.db_path).slots(START, START + HOUR)
    assert [row.slot_start for row in rows] == [START, START + HALF_HOUR]
    for row in rows:
        for name, kwh in {"grid_import": 4, "grid_export": 1, "ev_charge": .5, "v2l": 1}.items():
            assert row.components[name].kwh == pytest.approx(kwh)
            assert row.components[name].provenance == "hourly_backfill"
        assert row.components["solar_generation"].reason == "not_configured"
        assert row.base_load_kwh == pytest.approx(2.5)
        assert row.house_load_kwh == pytest.approx(3)
        assert row.import_rate is row.export_rate is None
        assert row.exclusions == ("hourly_backfill",)
    result = compute_stats(rows, START, START + HOUR, UTC)
    assert result.energy_coverage == 1
    assert result.totals.components_kwh["grid_import"] == pytest.approx(8)


async def test_backfill_respects_first_recorder_slot_and_never_replaces_rows(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    cutoff = START + HOUR + HALF_HOUR
    await record_first_slot(settings, cutoff)
    ledger = EnergyLedger(settings.db_path)
    end = START + 3 * HOUR
    original = await ledger.slots(START, end)
    changes = [hour(start, change=2) for start in (START, START + HOUR, START + 2 * HOUR)]
    fake_recorder(monkeypatch, {"sensor.grid_import": ("kWh", changes)})
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await ledger.slots(START, end)
    assert [row.slot_start for row in rows] == [START, START + HALF_HOUR, START + HOUR, cutoff]
    assert rows[-1:] == original
    for row in changes:
        row["change"] = 8
    assert await ledger_backfill.backfill_energy_ledger(settings)
    assert await ledger.slots(START, end) == rows


async def test_backfill_respects_earlier_recorder_row_arriving_during_read(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    await record_first_slot(settings, START + 2 * HOUR)
    fake_recorder(monkeypatch, {"sensor.grid_import": ("kWh", [])})

    async def overlapping_record(*args, **kwargs):
        await record_first_slot(settings, START + HOUR)
        return [hour(START, change=2), hour(START + HOUR, change=2)]

    monkeypatch.setattr(ledger_backfill, "statistics_during_period", overlapping_record)
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await EnergyLedger(settings.db_path).slots(START, START + 2 * HOUR)
    assert [row.slot_start for row in rows] == [START, START + HALF_HOUR, START + HOUR]
    assert rows[-1].import_rate == .3


@pytest.mark.parametrize("bad_change", [None, "bad", "nan", "inf", -1, True, 10**400])
async def test_invalid_component_leaves_both_slots_absent_and_grid_gap_skips_hour(
    tmp_path, monkeypatch, bad_change,
):
    settings = settings_for(tmp_path, derive_solar_generation_entity="sensor.solar")
    await record_first_slot(settings, START + 2 * HOUR)
    fake_recorder(monkeypatch, {
        "sensor.grid_import": ("kWh", [hour(START, change=2)]),
        "sensor.solar": ("kWh", [hour(START, change=bad_change), hour(START + HOUR, change=2)]),
    })
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await EnergyLedger(settings.db_path).slots(START, START + 2 * HOUR)
    assert [row.slot_start for row in rows] == [START, START + HALF_HOUR]
    for row in rows:
        flow = row.components["solar_generation"]
        assert flow.kwh is None
        assert (flow.provenance, flow.reason) == ("absent", "hourly_statistic_missing")
        assert row.base_load_kwh is row.house_load_kwh is None


async def test_implausible_hourly_row_rejects_both_half_hours(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    await record_first_slot(settings, START + 2 * HOUR)
    fake_recorder(monkeypatch, {
        "sensor.grid_import": ("kWh", [hour(START, change="27800"), hour(START + HOUR, change=2)]),
    })
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await EnergyLedger(settings.db_path).slots(START, START + 2 * HOUR)
    assert [row.slot_start for row in rows] == [
        START, START + HALF_HOUR, START + HOUR, START + HOUR + HALF_HOUR,
    ]
    for row in rows[:2]:
        flow = row.components["grid_import"]
        assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")
        assert row.base_load_kwh is row.house_load_kwh is None
    for row in rows[2:]:
        assert row.components["grid_import"].kwh == pytest.approx(1)
        assert row.components["grid_import"].provenance == "hourly_backfill"
    assert caplog.messages == ["Energy ledger rejected an implausible interval"]
    assert "27800" not in caplog.text


async def test_no_recorder_row_leaves_database_uncreated(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)

    async def unexpected_read(*args, **kwargs):
        pytest.fail("Backfill read HA before there was a recorder row")

    monkeypatch.setattr(ledger_backfill, "list_statistic_ids", unexpected_read)
    assert not await ledger_backfill.backfill_energy_ledger(settings)
    assert not await asyncio.to_thread(Path(settings.db_path).exists)


async def test_backfill_failure_is_logged_without_payload_and_writes_no_rows(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    await record_first_slot(settings, START + HOUR)
    fake_recorder(monkeypatch, {"sensor.grid_import": ("kWh", [hour(START, change=2)])})

    async def broken_read(*args, **kwargs):
        raise RuntimeError("private recorder payload")

    warnings = []
    monkeypatch.setattr(ledger_backfill, "statistics_during_period", broken_read)
    monkeypatch.setattr(ledger_backfill, "log", SimpleNamespace(warning=warnings.append))
    assert not await ledger_backfill.backfill_energy_ledger(settings)
    assert warnings == ["Energy ledger backfill failed"]
    assert await EnergyLedger(settings.db_path).slots(START, START + HOUR) == []


def test_backfill_module_has_no_ha_write_path():
    from tests.test_energy_ledger import test_statistics_modules_have_no_write_path

    test_statistics_modules_have_no_write_path("ledger_backfill")
    tree = ast.parse(Path(ledger_backfill.__file__).read_text())
    assert not any(
        getattr(node, attribute, None) == "import_statistics"
        for node in ast.walk(tree) for attribute in ("name", "attr", "id")
    )


@pytest.mark.usefixtures("legacy_solis_hub")
async def test_daemon_backfill_never_delays_guard_and_retries_failure_after_an_hour(
    tmp_path, monkeypatch,
):
    settings = settings_for(tmp_path, grid_power_entity="sensor.grid")
    seed_authority(settings, "observe")
    started, cancelled, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def backfill(tick_settings):
        calls.append(tick_settings)
        if len(calls) == 1:
            return False
        started.set()
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    with respx.mock:
        stub_control(monkeypatch)
        first = next(scheduler.run_once.side_effect)
        monkeypatch.setattr(scheduler, "run_once", AsyncMock(return_value=first))
        monkeypatch.setattr(scheduler, "backfill_energy_ledger", backfill)
        guard = AsyncMock(return_value=None)
        monkeypatch.setattr(scheduler, "guard_tick", guard)
        monkeypatch.setattr(scheduler, "_charger_supports_live_rate", AsyncMock(return_value=True))
        tz = ZoneInfo(settings.timezone)
        # The failure is seen on the next tick (30 min), so the retry is due at 90 min.
        offsets = (0, 30, 89, 91, 92)
        ticks = [(TICK + timedelta(minutes=m)).astimezone(tz) for m in offsets]
        stop = daemon_ticks(monkeypatch, ticks)
        ready.set()
        wait_at_tick_end(monkeypatch, ready)
        with pytest.raises(stop):
            await asyncio.wait_for(run_daemon(settings, poll_seconds=123), timeout=3)
    assert guard.await_count == len(offsets)
    assert len(calls) == 2
    assert started.is_set() and cancelled.is_set()


async def test_backfill_stops_before_day_close_fill_rows_and_leaves_them_unchanged(
    tmp_path, monkeypatch,
):
    settings = settings_for(tmp_path)
    await record_first_slot(settings, START + 3 * HOUR)
    filled = {
        START + HOUR: ComponentFlow(kwh=3, provenance="statistics_fill"),
        START + HOUR + HALF_HOUR: ComponentFlow(kwh=3, provenance="distributed"),
    }
    async with aiosqlite.connect(settings.db_path) as db:
        for instant, flow in filled.items():
            row = LedgerSlot(slot_start=instant, components={"grid_import": flow},
                             exclusions=("no_rate_capture",))
            await db.execute("INSERT INTO energy_ledger_slots VALUES (?, ?)",
                             (instant.isoformat(), row.model_dump_json()))
        await db.commit()
    ledger = EnergyLedger(settings.db_path)
    before = await ledger.slots(START + HOUR, START + 4 * HOUR)
    changes = [hour(START + i * HOUR, change=2) for i in range(4)]
    fake_recorder(monkeypatch, {"sensor.grid_import": ("kWh", changes)})
    assert await ledger_backfill.backfill_energy_ledger(settings)
    rows = await ledger.slots(START, START + 4 * HOUR)
    assert [row.slot_start for row in rows[:2]] == [START, START + HALF_HOUR]
    assert all(row.exclusions == ("hourly_backfill",) for row in rows[:2])
    assert rows[2:] == before
