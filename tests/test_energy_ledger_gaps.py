"""Reconcile boundary gaps through fake HA counters and recorder statistics."""

import asyncio
from datetime import timedelta

import pytest
import respx

from ha_spark.energy import energy_ledger
from ha_spark.energy.energy_ledger import EnergyLedger, RateEvidence
from tests.test_energy_ledger import CLOSE, HALF_HOUR, counter, settings_for
from tests.test_readiness_ledger import START

END = START + timedelta(days=1)
SETTLED = END + timedelta(hours=1)
RATES = RateEvidence(import_rate=0.2, export_rate=0.05, revision_id="A")


@pytest.mark.parametrize("reason", ["counter_reset", "source_changed"])
@respx.mock
async def test_gap_does_not_distribute_reset_or_changed_source(tmp_path, reason):
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    tick = CLOSE + HALF_HOUR
    if reason == "counter_reset":
        counter("sensor.grid_import", ("10", START), ("2", tick))
        next_settings = settings
    else:
        counter("sensor.grid_import", ("10", START))
        counter("sensor.replacement", ("12", tick))
        next_settings = settings.model_copy(update={
            "derive_grid_import_entity": "sensor.replacement",
        })
    await ledger.record_boundary(settings, START, RATES)
    await ledger.record_boundary(next_settings, tick, RATES)
    [row] = await ledger.slots(START, tick)
    assert row.slot_start == CLOSE
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", reason)


@respx.mock
async def test_distribution_updates_gap_but_preserves_other_boundary_flows(tmp_path):
    settings = settings_for(tmp_path, derive_grid_export_entity="sensor.grid_export")
    ledger = EnergyLedger(settings.db_path)
    tick = CLOSE + HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("unavailable", CLOSE), ("16", tick))
    counter("sensor.grid_export", ("2", START), ("3", CLOSE), ("4", tick))
    for boundary in (START, CLOSE, tick):
        await ledger.record_boundary(settings, boundary, RATES)
    rows = await ledger.slots(START, tick)
    assert len(rows) == 2
    for row in rows:
        assert row.components["grid_import"].kwh == 3
        assert row.components["grid_import"].provenance == "distributed"
        assert row.components["grid_export"].kwh == 1
        assert row.components["grid_export"].provenance == "boundary"
        assert row.base_load_kwh == row.house_load_kwh == 2
        assert (row.import_rate, row.export_rate, row.revision_id) == (0.2, 0.05, "A")


@respx.mock
async def test_inserted_distributed_row_keeps_unconfigured_components_and_loads(tmp_path):
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    tick = CLOSE + HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("16", tick))
    for boundary in (START, tick):
        await ledger.record_boundary(settings, boundary, RATES)
    inserted, closed = await ledger.slots(START, tick)
    assert inserted.components["grid_export"].reason == "not_configured"
    assert inserted.exclusions == ("no_rate_capture",)
    assert inserted.house_load_kwh == closed.house_load_kwh == 3


def five_minutes(start, *, count=6, **values):
    return [
        {"start": (start + timedelta(minutes=5 * i)).timestamp() * 1000, **values}
        for i in range(count)
    ]


def fake_recorder(monkeypatch, units, read):
    async def metadata(*args, **kwargs):
        return [
            {"statistic_id": entity, "statistics_unit_of_measurement": unit}
            for entity, unit in units.items()
        ]

    async def statistics(url, token, entity, start, *, period, **kwargs):
        assert period == "5minute"
        return read(entity, start)

    monkeypatch.setattr(energy_ledger, "list_statistic_ids", metadata)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", statistics)


@respx.mock
async def test_day_close_fills_gaps_without_overwriting_boundary_or_rates(tmp_path, monkeypatch):
    settings = settings_for(
        tmp_path, timezone="UTC", derive_grid_export_entity="sensor.grid_export",
        derive_invert_grid_export=True, v2l_power_entity="sensor.v2l_power",
    )
    ledger = EnergyLedger(settings.db_path)
    third = CLOSE + HALF_HOUR
    tick = third + HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("13", CLOSE), ("17", tick))
    counter("sensor.grid_export", ("10", START), ("9", CLOSE), ("7", tick))
    counter("sensor.v2l_power", *(("1000", t, "W") for t in (START, CLOSE, tick)))
    for boundary in (START, CLOSE, tick):
        await ledger.record_boundary(settings, boundary, RATES)
    rows = {
        "sensor.grid_import": [
            row for t in (START, CLOSE, third) for row in five_minutes(t, change="1000")
        ] + five_minutes(tick, count=5, change="1000"),
        "sensor.grid_export": five_minutes(third, change="-500"),
        "sensor.v2l_power": five_minutes(START, mean="2000"),
    }
    fake_recorder(monkeypatch, {
        "sensor.grid_import": "Wh", "sensor.grid_export": "Wh", "sensor.v2l_power": "W",
    }, lambda entity, start: rows[entity] if start == START else [])
    await energy_ledger.fill_closed_days(settings, SETTLED - timedelta(seconds=1))
    before = await ledger.slots(START, tick)
    assert before[2].components["grid_import"].provenance == "distributed"
    assert before[0].components["v2l"].kwh is None

    await energy_ledger.fill_closed_days(settings, SETTLED)
    first, second, last = await ledger.slots(START, END)
    assert first.components["grid_import"].kwh == 3
    assert first.components["grid_import"].provenance == "boundary"
    assert first.components["v2l"].kwh == pytest.approx(1)
    assert first.components["v2l"].provenance == "statistics_fill"
    assert first.base_load_kwh == first.house_load_kwh == 2
    assert second.components["grid_import"].kwh == 6
    assert second.components["grid_import"].provenance == "statistics_fill"
    assert second.components["grid_export"].provenance == "distributed"
    assert second.import_rate is second.export_rate is None
    assert second.exclusions == ("no_rate_capture",)
    # The export gap's fill covers only one of its two slots: both keep their
    # distributed shares, which still sum to the counter's 2 kWh delta.
    assert last.components["grid_export"].kwh == 1
    assert last.components["grid_export"].provenance == "distributed"
    assert second.components["grid_export"].kwh + last.components["grid_export"].kwh == 2
    assert last.house_load_kwh == last.base_load_kwh == 5
    assert (last.import_rate, last.export_rate, last.revision_id) == (0.2, 0.05, "A")


@pytest.mark.parametrize(("unit", "change"), [
    ("kWh", "-1"), ("kWh", "nan"), ("kWh", "broken"), ("invalid", "1"),
])
async def test_invalid_statistics_do_not_create_a_measurement(tmp_path, monkeypatch, unit, change):
    settings = settings_for(tmp_path, timezone="UTC")
    fake_recorder(
        monkeypatch, {"sensor.grid_import": unit},
        lambda entity, start: five_minutes(START, change=change) if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    assert await EnergyLedger(settings.db_path).slots(START, END) == []


@pytest.mark.parametrize("bad", [-5, True])
async def test_one_invalid_interval_blocks_the_half_hour_fill(tmp_path, monkeypatch, bad):
    settings = settings_for(tmp_path, timezone="UTC")
    rows = five_minutes(START, count=5, change=1) + [
        {"start": (START + timedelta(minutes=25)).timestamp() * 1000, "change": bad},
    ]
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: rows if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    assert await EnergyLedger(settings.db_path).slots(START, END) == []


@respx.mock
async def test_boundary_committed_during_statistics_read_keeps_precedence(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, timezone="UTC")
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("13", CLOSE))
    await ledger.record_boundary(settings, START, RATES)
    reading, release = asyncio.Event(), asyncio.Event()
    fake_recorder(monkeypatch, {"sensor.grid_import": "kWh"}, lambda entity, start: [])

    async def statistics(url, token, entity, start, **kwargs):
        if start != START:
            return []
        reading.set()
        await release.wait()
        return five_minutes(START, change="2")

    monkeypatch.setattr(energy_ledger, "statistics_during_period", statistics)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(energy_ledger.fill_closed_days(settings, SETTLED))
        await asyncio.wait_for(reading.wait(), timeout=2)
        await ledger.record_boundary(settings, CLOSE, RATES)
        release.set()
    [row] = await ledger.slots(START, END)
    flow = row.components["grid_import"]
    assert (flow.provenance, flow.kwh) == ("boundary", 3)
    assert row.import_rate == RATES.import_rate


async def test_implausible_five_minute_row_rejects_its_half_hour(
    tmp_path, monkeypatch, caplog,
):
    # A counter reset's recovery is one five-minute row the size of the
    # lifetime reading: the half-hour becomes a rejected gap, never a
    # partially summed value.
    settings = settings_for(tmp_path, timezone="UTC")
    spike = five_minutes(START, count=5, change="1000") + [
        {"start": (START + timedelta(minutes=25)).timestamp() * 1000, "change": "27800"},
    ]
    rows = spike + five_minutes(START + HALF_HOUR, change="1000")
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "Wh"},
        lambda entity, start: rows if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    first, second = await EnergyLedger(settings.db_path).slots(START, END)
    flow = first.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")
    assert first.house_load_kwh is None
    assert (second.components["grid_import"].kwh,
            second.components["grid_import"].provenance) == (6, "statistics_fill")
    assert caplog.messages == ["Energy ledger rejected an implausible interval"]
    assert "27800" not in caplog.text


@pytest.mark.parametrize("complete", [True, False])
async def test_failed_day_read_retries_only_within_retention(
    tmp_path, monkeypatch, caplog, complete,
):
    # 12 kWh in one half-hour needs a 30 kW site bound to stay plausible data.
    settings = settings_for(tmp_path, timezone="UTC", supply_max_current_a=100)
    ledger = EnergyLedger(settings.db_path)
    fail = True

    def read(entity, start):
        if start != START:
            return []
        if fail:
            raise RuntimeError("private upstream body")
        return five_minutes(START, change="2")

    fake_recorder(monkeypatch, {"sensor.grid_import": "kWh"}, read)
    near_expiry = START + timedelta(days=10, minutes=-30)
    now = SETTLED if complete else near_expiry
    await energy_ledger.fill_closed_days(settings, now)
    assert await ledger.slots(START, END) == []
    assert caplog.messages == ["Energy ledger fill failed"]
    assert "private upstream body" not in caplog.text
    fail = False
    await energy_ledger.fill_closed_days(settings, now + timedelta(minutes=15))
    assert await ledger.slots(START, END) == []
    await energy_ledger.fill_closed_days(settings, now + timedelta(hours=1))
    rows = await ledger.slots(START, END)
    if complete:
        [row] = rows
        assert row.components["grid_import"].kwh == 12
        assert row.components["grid_import"].provenance == "statistics_fill"
        assert row.import_rate is row.export_rate is None
        assert row.exclusions == ("no_rate_capture",)
        assert row.house_load_kwh == row.base_load_kwh == 12
    else:
        assert rows == []
