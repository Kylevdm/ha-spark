"""Boundary evidence through fake HA states and the public SQLite reader."""

import ast
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import energy_ledger
from ha_spark.energy.energy_ledger import EnergyLedger, capture_rates
from ha_spark.energy.models import FlexibilityEvent, FreeSession, PricePoint
from ha_spark.energy.stats import summarise
from tests.test_readiness_ledger import START, revision

HALF_HOUR = timedelta(minutes=30)
CLOSE = START + HALF_HOUR


def settings_for(tmp_path, **kwargs):
    return Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ledger.db"),
        derive_grid_import_entity="sensor.grid_import", **kwargs,
    )


def state(entity, value, when, unit="kWh"):
    return {
        "entity_id": entity, "state": value, "last_reported": when.isoformat(),
        "attributes": {"unit_of_measurement": unit},
    }


def counter(entity, *samples):
    return respx.get(f"http://ha.test/api/states/{entity}").mock(side_effect=[
        respx.MockResponse(200, json=state(entity, *sample)) for sample in samples
    ])


@respx.mock
async def test_boundary_delta_and_inverted_export_are_write_once(tmp_path):
    settings = settings_for(tmp_path, derive_grid_export_entity="sensor.grid_export",
                            derive_invert_grid_export=True, derive_ev_charge_entity="sensor.ev")
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10000", START, "Wh"), ("14000", CLOSE, "Wh"),
            ("20000", CLOSE, "Wh"))
    counter("sensor.grid_export", ("5", START), ("4", CLOSE), ("3", CLOSE))
    counter("sensor.ev", ("20", START), ("22", CLOSE), ("23", CLOSE))
    await ledger.record_boundary(settings, START, capture_rates(settings, START, None))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, revision()))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, revision("B", .4)))
    first, row = await ledger.slots(START - HALF_HOUR, CLOSE)
    assert first.components["grid_import"].reason == "no_previous_reading"
    assert first.base_load_kwh is None
    assert row.slot_start == START
    assert row.components["grid_import"].kwh == 4
    assert row.components["grid_import"].provenance == "boundary"
    assert row.components["grid_export"].kwh == 1
    assert row.components["solar_generation"].reason == "not_configured"
    assert row.base_load_kwh == 1
    assert row.house_load_kwh == 3
    assert not row.negative_clamped
    assert (row.import_rate, row.export_rate, row.revision_id) == (0.1, 0.05, "A")


def daily_revision():
    rev = revision()
    inputs = replace(rev.run.inputs, load_slots=None, horizon_start=None)
    schedule = replace(rev.run.schedule, prices=(), cheap_fracs=())
    return replace(rev, run=replace(rev.run, inputs=inputs, schedule=schedule))


@pytest.mark.parametrize(("offset", "window_start", "window_end", "rate"), [
    (-3, "23:30", "01:00", 0.125),  # Window opens at 23:30 on the household clock.
    (0, "23:30", "01:00", 0.125),  # After midnight, at the inclusive window end.
    (1, "23:30", "01:00", 0.25),
    (2, "02:00", "03:00", 0.125),  # A window that doesn't wrap.
])
@respx.mock
async def test_fixed_daily_tariff_prices_closed_slots(
    tmp_path, offset, window_start, window_end, rate,
):
    start = START + offset * HALF_HOUR
    close = start + HALF_HOUR
    settings = settings_for(
        tmp_path, tariff_provider="fixed", timezone="Europe/London",
        charge_window_start=window_start, charge_window_end=window_end,
        rate_offpeak_gbp_kwh=0.125, rate_peak_gbp_kwh=0.25, rate_export_gbp_kwh=0.0625,
        derive_grid_export_entity="sensor.grid_export",
    )
    ledger = EnergyLedger(settings.db_path)
    rev = daily_revision()
    counter("sensor.grid_import", ("10", start), ("13", close), ("13", close))
    counter("sensor.grid_export", ("2", start), ("3", close), ("3", close))
    await ledger.record_boundary(settings, start, capture_rates(settings, start, None))
    await ledger.record_boundary(settings, close, capture_rates(settings, close, rev))
    changed = settings.model_copy(update={"rate_export_gbp_kwh": 0.5})
    await ledger.record_boundary(changed, close, capture_rates(changed, close, rev))

    [row] = await ledger.slots(start, close)
    assert (row.import_rate, row.export_rate, row.revision_id) == (rate, 0.0625, "A")
    assert row.exclusions == ()
    assert row.import_session is row.session_id is None
    [day] = summarise([row], start, close, ZoneInfo(settings.timezone), "day").buckets
    assert day.totals.cost_coverage == 1
    assert day.totals.actual_cost == 3 * rate - 0.0625
    assert day.totals.no_battery_cost == 2 * rate
    assert day.totals.battery_savings == 0.0625 - rate


@pytest.mark.parametrize("overrides", [
    {"tariff_provider": "dynamic"},
    {"tariff_provider": "octopus_intelligent"},
    {"charge_window_end": "25:00"},
    {"timezone": "not/a/timezone"},
    {"rate_export_gbp_kwh": -0.1},
    {"rate_peak_gbp_kwh": float("inf")},
])
def test_daily_tariff_leaves_non_fixed_or_invalid_rates_unpriced(tmp_path, overrides):
    settings = settings_for(tmp_path, **({"tariff_provider": "fixed"} | overrides))
    rates = capture_rates(settings, CLOSE, daily_revision())
    assert rates.import_rate is rates.export_rate is None
    assert rates.revision_id == "A"
    assert rates.exclusions == ("no_tariff_geometry",)


@pytest.mark.parametrize(("value", "when", "unit", "reason"), [
    ("9", CLOSE, "kWh", "counter_reset"),
    ("nan", CLOSE, "kWh", "not_finite"),
    ("broken", CLOSE, "kWh", "not_finite"),
    ("unavailable", CLOSE, "kWh", "unavailable"),
    ("12", START, "kWh", "stale"),
    ("12", CLOSE, "W", "not_a_counter"),
    ("12", CLOSE, None, "not_a_counter"),
    # Reported after the boundary's sampling window: too late for this slot.
    ("12", CLOSE + timedelta(minutes=5, seconds=1), "kWh", "future_report_time"),
    ("-1", CLOSE, "kWh", "invalid_kwh"),
])
@respx.mock
async def test_unusable_counter_is_absent_not_zero(tmp_path, value, when, unit, reason):
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), (value, when, unit))
    for tick in (START, CLOSE):
        await ledger.record_boundary(settings, tick, capture_rates(settings, tick, revision()))
    [row] = await ledger.slots(START, CLOSE)
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", reason)
    assert row.house_load_kwh is row.base_load_kwh is None


@respx.mock
async def test_missed_boundary_distributes_and_next_slot_uses_reading(tmp_path):
    settings = settings_for(tmp_path)
    ticks = (START, START + 2 * HALF_HOUR, START + 3 * HALF_HOUR)
    counter("sensor.grid_import", ("5", ticks[0]), ("10", ticks[1]), ("11", ticks[2]))
    for tick in ticks:
        await EnergyLedger(settings.db_path).record_boundary(
            settings, tick, capture_rates(settings, tick, revision()),
        )
    rows = await EnergyLedger(settings.db_path).slots(START, ticks[-1])
    assert [r.slot_start for r in rows] == [START, CLOSE, ticks[1]]
    for row in rows[:2]:
        flow = row.components["grid_import"]
        assert (flow.kwh, flow.provenance) == (2.5, "distributed")
    assert rows[0].import_rate is rows[0].export_rate is None
    assert rows[0].exclusions == ("no_rate_capture",)
    assert rows[1].import_rate == 0.1  # Captured at close, usable only after a fill.
    assert rows[2].components["grid_import"].kwh == 1



@respx.mock
async def test_report_during_the_ticks_control_work_is_still_the_boundary_reading(tmp_path):
    # The ledger reads after replan and control steps; a sensor that reported in
    # between is inside the boundary's sampling window, not from the future.
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("12", CLOSE + timedelta(seconds=20)))
    for tick in (START, CLOSE):
        await ledger.record_boundary(settings, tick, capture_rates(settings, tick, revision()))
    [row] = await ledger.slots(START, CLOSE)
    assert (row.components["grid_import"].kwh, row.components["grid_import"].provenance) == (
        2, "boundary")

@pytest.mark.parametrize("provider", ["dynamic", "octopus_intelligent"])
@respx.mock
async def test_provider_fallback_is_not_history(tmp_path, provider):
    settings = settings_for(tmp_path, tariff_provider=provider)
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, revision()))
    [row] = await ledger.slots(START, CLOSE)
    assert row.import_rate is row.export_rate is None
    assert "provider_rate_unavailable" in row.exclusions


@pytest.mark.parametrize(("points", "priced"), [
    (((START, CLOSE),), True),
    (((START - HALF_HOUR, CLOSE + HALF_HOUR),), True),
    (((START + timedelta(minutes=10), CLOSE),), False),
    (((START, START + timedelta(minutes=15)), (START + timedelta(minutes=15), CLOSE)), False),
])
@respx.mock
async def test_intelligent_unit_rates_price_only_a_wholly_covered_slot(tmp_path, points, priced):
    settings = settings_for(tmp_path, tariff_provider="octopus_intelligent")
    rev = revision()
    inputs = replace(rev.run.inputs, dynamic_prices=tuple(
        PricePoint(start, end, 0.1) if i == 0 else PricePoint(start, end, 0.3)
        for i, (start, end) in enumerate(points)
    ))
    rev = replace(rev, run=replace(rev.run, inputs=inputs))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
    [row] = await ledger.slots(START, CLOSE)
    if priced:
        assert (row.import_rate, row.exclusions) == (0.1, ())
    else:
        assert row.import_rate is row.export_rate is None
        assert row.exclusions == ("provider_rate_unavailable",)

@pytest.mark.parametrize("kind", ["free_electricity", "happy_hour"])
@respx.mock
async def test_rates_and_session_come_from_the_mid_slot_revision(tmp_path, kind):
    settings = settings_for(tmp_path, tariff_provider="dynamic")
    booking_end = CLOSE + HALF_HOUR
    rev = revision("mid-slot")
    schedule = replace(rev.run.schedule, prices=(0.23, 0.4), export_prices=(0.17, 0.18),
                       explicit_import_prices=(0.23, None), import_overlays=(kind, None),
                       import_sessions=(FreeSession(kind, "booking", START, booking_end), None))
    rev = replace(rev, issued_at=START + timedelta(minutes=12),
                  run=replace(rev.run, schedule=schedule))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("12", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
    [row] = await ledger.slots(START, CLOSE)
    assert (row.import_rate, row.export_rate, row.revision_id) == (0.23, 0.17, "mid-slot")
    assert row.import_session == kind
    if kind == "happy_hour":
        assert row.session_id == "booking"
        assert (row.session_start, row.session_end) == (START, booking_end)
    else:
        assert row.session_id is row.session_start is row.session_end is None


@respx.mock
async def test_export_event_keeps_supplier_export_rate(tmp_path):
    settings = settings_for(tmp_path)
    rev = revision()
    event = FlexibilityEvent(START, CLOSE, "export", START, 1.5)
    rev = replace(rev, run=replace(rev.run, inputs=replace(rev.run.inputs, flexibility_event=event),
                                  schedule=replace(rev.run.schedule, export_prices=(1.5,))))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("12", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
    [row] = await ledger.slots(START, CLOSE)
    assert row.export_rate == 0.05


@respx.mock
async def test_changed_counter_source_excludes_only_its_component(tmp_path):
    settings = settings_for(tmp_path, derive_battery_charge_entity="sensor.battery_charge")
    next_settings = settings.model_copy(update={
        "derive_battery_charge_entity": "sensor.replacement",
    })
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("12", CLOSE))
    counter("sensor.battery_charge", ("8", START))
    counter("sensor.replacement", ("10", CLOSE))
    await ledger.record_boundary(settings, START, capture_rates(settings, START, None))
    await ledger.record_boundary(next_settings, CLOSE, capture_rates(settings, CLOSE, revision()))
    [row] = await ledger.slots(START, CLOSE)
    assert row.components["grid_import"].kwh == 2
    assert row.components["battery_charge"].reason == "source_changed"
    assert row.base_load_kwh is row.house_load_kwh is None


@respx.mock
async def test_negative_balance_is_clamped_before_adding_ev_charge(tmp_path):
    settings = settings_for(tmp_path, derive_ev_charge_entity="sensor.ev")
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("11", CLOSE))
    counter("sensor.ev", ("8", START), ("10", CLOSE))
    await ledger.record_boundary(settings, START, capture_rates(settings, START, None))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, revision()))
    [row] = await ledger.slots(START, CLOSE)
    assert row.negative_clamped
    assert (row.base_load_kwh, row.house_load_kwh) == (0, 2)


@respx.mock
async def test_v2l_power_is_not_a_boundary_measurement(tmp_path):
    settings = settings_for(tmp_path, v2l_power_entity="sensor.v2l_power")
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("12", CLOSE))
    counter("sensor.v2l_power", ("1000", START, "W"), ("1000", CLOSE, "W"))
    for tick in (START, CLOSE):
        await ledger.record_boundary(settings, tick, capture_rates(settings, tick, revision()))
    [row] = await ledger.slots(START, CLOSE)
    flow = row.components["v2l"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "not_a_counter")
    assert row.components["grid_import"].kwh == 2
    # An absent V2L never blocks the loads: it is never added to house load.
    assert row.house_load_kwh == row.base_load_kwh == 2


@respx.mock
async def test_late_tick_does_not_open_a_ledger(tmp_path):
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    late = CLOSE + timedelta(minutes=5)
    await ledger.record_boundary(settings, late, capture_rates(settings, late, revision()))
    assert await ledger.slots(START, CLOSE) == []
    assert not respx.calls


@respx.mock
async def test_recorder_failure_leaves_gap_and_logs_no_payload(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("12345", CLOSE))

    def fail(*args, **kwargs):
        raise OSError("sensor.grid_import 12345")

    with monkeypatch.context() as patch:
        patch.setattr(energy_ledger.aiosqlite, "connect", fail)
        await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, revision()))
    assert caplog.messages == ["Energy ledger recording failed"]
    assert "sensor.grid_import" not in caplog.text and "12345" not in caplog.text
    assert await ledger.slots(START, CLOSE) == []


async def test_reading_a_missing_ledger_does_not_create_it(tmp_path):
    path = tmp_path / "missing.db"
    assert await EnergyLedger(str(path)).slots(START, CLOSE) == []
    assert not path.exists()


@pytest.mark.parametrize("name", ["energy_ledger", "stats", "axle_events"])
def test_statistics_modules_have_no_write_path(name):
    path = Path(energy_ledger.__file__).with_name(f"{name}.py")
    forbidden = (
        "ha_spark.devices", "ha_spark.authority", "ha_spark.transitions",
        "ha_spark.mode_preconditions", "ha_spark.daemon_channel", "ha_spark.api.commands",
        "ha_spark.api.native_apply", "ha_spark.api.contract", "ha_spark.energy.supply_guard",
        "ha_spark.energy.soc_monitor",
        "ha_spark.energy.v2l", "ha_spark.energy.scheduler",
    )
    if name == "stats":
        forbidden += ("ha_spark.ha",)
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            parent = f"{node.module}." if node.module else ""
            imports = [parent + alias.name for alias in node.names]
        else:
            imports = []
        assert not any(module.startswith(forbidden) for module in imports)
        assert not any(module.split(".")[-1] == "call_service" for module in imports)
        assert not (isinstance(node, ast.Attribute) and node.attr == "call_service")
        assert not (isinstance(node, ast.Name) and node.id == "call_service")


def test_revision_stamped_after_boundary_still_prices_the_slot_its_horizon_covers(tmp_path):
    settings = settings_for(tmp_path)
    late = replace(revision(), issued_at=CLOSE + timedelta(seconds=3))
    rates = capture_rates(settings, CLOSE, late)
    assert (rates.import_rate, rates.revision_id, rates.exclusions) == (0.1, "A", ())


@respx.mock
async def test_unconvertible_report_time_excludes_only_that_component(tmp_path):
    settings = settings_for(tmp_path, derive_solar_generation_entity="sensor.solar")
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("10", START), ("12", CLOSE))
    respx.get("http://ha.test/api/states/sensor.solar").respond(200, json={
        "entity_id": "sensor.solar", "state": "5",
        "last_reported": "9999-12-31T23:59:59-01:00",
        "attributes": {"unit_of_measurement": "kWh"},
    })
    for tick in (START, CLOSE):
        await ledger.record_boundary(settings, tick, capture_rates(settings, tick, revision()))
    [row] = await ledger.slots(START, CLOSE)
    assert row.components["grid_import"].kwh == 2
    assert (row.components["solar_generation"].kwh,
            row.components["solar_generation"].reason) == (None, "unreadable")
    assert row.base_load_kwh is None
