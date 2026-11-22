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


async def test_an_off_tick_implausible_row_does_not_reject_its_half_hour(
    tmp_path, monkeypatch, caplog,
):
    # measurement_series sums only the six aligned ticks: a huge row at :01
    # never contributes to the half-hour, so it must not reject it either.
    settings = settings_for(tmp_path, timezone="UTC")
    rows = five_minutes(START, change="1000") + [
        {"start": (START + timedelta(minutes=1)).timestamp() * 1000, "change": "27800"},
    ]
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "Wh"},
        lambda entity, start: rows if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    [row] = await EnergyLedger(settings.db_path).slots(START, END)
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance) == (6, "statistics_fill")
    assert caplog.messages == []


@respx.mock
async def test_a_rejected_fill_keeps_every_share_of_a_distributed_run(tmp_path, monkeypatch):
    # A rejected half-hour is a gap, not a measurement: the run it sits in is
    # not wholly covered, so both distributed shares stay, summing to the
    # counter's 4 kWh delta rather than mixing one fill with one share.
    settings = settings_for(tmp_path, timezone="UTC")
    ledger = EnergyLedger(settings.db_path)
    late = START + 2 * HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("14", late))
    for tick in (START, late):
        await ledger.record_boundary(settings, tick, RATES)
    rows = (
        five_minutes(START, change="1000")
        + five_minutes(CLOSE, count=5, change="1000")
        + [{"start": (CLOSE + timedelta(minutes=25)).timestamp() * 1000,
            "change": "27800"}]
    )
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "Wh"},
        lambda entity, start: rows if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    first, second = await ledger.slots(START, late)
    for row in (first, second):
        flow = row.components["grid_import"]
        assert (flow.kwh, flow.provenance) == (2, "distributed")
        assert row.house_load_kwh == row.base_load_kwh == 2


async def test_a_five_minute_mean_exactly_at_the_bound_is_filled(tmp_path, monkeypatch):
    # 28 A x 230 V = 6.44 kW import with the export limit below it: the bound
    # is 8.05 kW, and a mean of exactly 8.05 kW must be filled, not rejected
    # by float rounding on either side of the comparison.
    settings = settings_for(
        tmp_path, timezone="UTC", supply_max_current_a=28, supply_voltage_v=230,
    )
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kW"},
        lambda entity, start: five_minutes(START, mean="8.05") if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    [row] = await EnergyLedger(settings.db_path).slots(START, END)
    flow = row.components["grid_import"]
    assert flow.provenance == "statistics_fill"
    assert flow.kwh == pytest.approx(8.05 / 2)


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


@respx.mock
async def test_boundary_after_a_fill_replaces_it_and_keeps_its_reading(tmp_path, monkeypatch):
    # The fill reached the slot before its boundary did: the boundary value
    # replaces the fill, and its counter reading still becomes the next
    # baseline, so the following slot is a one-slot boundary delta.
    settings = settings_for(tmp_path, timezone="UTC")
    ledger = EnergyLedger(settings.db_path)
    tick = CLOSE + HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("13", CLOSE), ("15", tick))
    await ledger.record_boundary(settings, START, RATES)
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "Wh"},
        lambda entity, start: five_minutes(START, change="400") if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    for boundary in (CLOSE, tick):
        await ledger.record_boundary(settings, boundary, RATES)
    first, second = await ledger.slots(START, tick)
    assert (first.components["grid_import"].kwh,
            first.components["grid_import"].provenance) == (3, "boundary")
    assert first.house_load_kwh == 3
    assert (second.components["grid_import"].kwh,
            second.components["grid_import"].provenance) == (2, "boundary")


@pytest.mark.parametrize(("reading", "third_flow"), [
    ("16", (1.5, "distributed", None)),
    ("14", (None, "absent", "rejected")),
])
@respx.mock
async def test_a_gap_closing_after_a_fill_spreads_only_the_unfilled_energy(
    tmp_path, monkeypatch, reading, third_flow,
):
    # A fill covered two slots of a still-open three-slot gap with 4.5 kWh: the
    # closing boundary spreads only what is left of the counter delta, and a
    # delta smaller than the fill already stored rejects the rest of the run.
    settings = settings_for(tmp_path, timezone="UTC")
    ledger = EnergyLedger(settings.db_path)
    third = CLOSE + HALF_HOUR
    tick = third + HALF_HOUR
    counter("sensor.grid_import", ("10", START), (reading, tick))
    await ledger.record_boundary(settings, START, RATES)
    rows = five_minutes(START, change="0.5") + five_minutes(CLOSE, change="0.25")
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: rows if start == START else [],
    )
    await energy_ledger.fill_closed_days(settings, SETTLED)
    await ledger.record_boundary(settings, tick, RATES)
    first, second, last = await ledger.slots(START, tick)
    assert (first.components["grid_import"].kwh,
            first.components["grid_import"].provenance) == (3, "statistics_fill")
    assert (second.components["grid_import"].kwh,
            second.components["grid_import"].provenance) == (1.5, "statistics_fill")
    flow = last.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == third_flow
