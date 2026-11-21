"""Pre-install energy history through fake HA statistics and the public ledger reader."""

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ha_spark.config import Settings
from ha_spark.energy import ledger_backfill
from ha_spark.energy.energy_ledger import EnergyLedger, RateEvidence
from ha_spark.energy.stats import compute_stats

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

    async def statistics(url, token, entity, start, *, period, **kwargs):
        assert period == "hour"
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
