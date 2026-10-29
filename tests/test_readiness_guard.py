"""Observe guard evidence uses GETs only and retains invalid samples."""
from dataclasses import replace
from datetime import timedelta

import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.models import PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.readiness_ledger import ReadinessLedger, record_guard
from tests.test_readiness_ledger import START, revision


@pytest.mark.parametrize("supply,valid,throttle,breach", [
    ("500", True, 1000, False),
    ("20000", True, 0, True),
    ("nan", False, None, None),
    ("unavailable", False, None, None),
])
@respx.mock
async def test_guard_samples_are_read_only(tmp_path, supply, valid, throttle, breach):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        battery_voltage_v=50, charge_window_start="00:00",
                        charge_window_end="06:00")
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": supply, "attributes": {}})
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": "20", "attributes": {}})
    await record_guard(settings, START, revision(), "observe")
    rows = await ReadinessLedger(settings.db_path).guards()
    assert len(rows) == 1
    assert rows[0].sample_valid is valid
    assert rows[0].target_w == 1000
    assert rows[0].throttle_w == throttle
    assert rows[0].breach is breach
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_guard_evidence_uses_revision_connection_not_current_settings(tmp_path):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        battery_voltage_v=50, charge_window_start="00:00",
                        charge_window_end="06:00", supply_max_current_a=75)
    a = revision()
    cfg = replace(a.run.cfg, supply_max_current_a=6)
    plan = compute_plan(a.run.inputs, cfg, a.run.schedule)
    a = replace(a, run=replace(a.run, plan=plan))
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "2000", "attributes": {}})
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": "20", "attributes": {}})
    await record_guard(settings, START, a, "observe")
    (row,) = await ReadinessLedger(settings.db_path).guards()
    assert row.sample_valid
    assert row.throttle_w == 440  # 6 A * 240 V minus 1000 W other load
    assert row.breach is False
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_guard_read_and_storage_failures_are_isolated(tmp_path, caplog):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        charge_window_start="00:00", charge_window_end="06:00")
    respx.get("http://ha.test/api/states/sensor.grid").respond(500)
    await record_guard(settings, START, revision(), "observe")
    rows = await ReadinessLedger(settings.db_path).guards()
    assert len(rows) == 1
    assert not rows[0].sample_valid
    assert rows[0].measured_import_w is None
    assert rows[0].breach is None
    settings = settings.model_copy(update={"db_path": str(tmp_path)})
    await record_guard(settings, START, revision(), "observe")
    assert "Readiness guard recording failed" in caplog.text
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_guard_without_matching_plan_records_invalid_coverage(tmp_path):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        charge_window_start="00:00", charge_window_end="06:00")
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "500", "attributes": {}},
    )
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": "20", "attributes": {}},
    )
    await record_guard(settings, START, None, "observe")
    await record_guard(settings, START + timedelta(days=2), revision(), "observe")
    rows = await ReadinessLedger(settings.db_path).guards()
    assert len(rows) == 2
    assert all(not r.sample_valid and r.target_w is None and r.breach is None for r in rows)
    assert all(r.measured_import_w == 500 for r in rows)


@respx.mock
async def test_bad_revision_geometry_still_records_invalid_guard_sample(tmp_path, monkeypatch):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        charge_window_start="00:00", charge_window_end="06:00")
    a = revision()

    def bad_geometry(*args):
        raise ValueError("invalid horizon")

    monkeypatch.setattr(PlannerInputs, "geometry_for", bad_geometry)
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "500", "attributes": {}},
    )
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": "20", "attributes": {}},
    )
    await record_guard(settings, START, a, "observe")
    (row,) = await ReadinessLedger(settings.db_path).guards()
    assert not row.sample_valid
    assert row.target_w is None
    assert row.throttle_w is None
    assert row.breach is None
    assert row.measured_import_w == 500
    assert all(c.request.method == "GET" for c in respx.calls)


@pytest.mark.parametrize("setpoint,valid", [("-20", False), ("0", True), ("20", True)])
@respx.mock
async def test_negative_setpoint_is_invalid_but_negative_grid_import_is_valid(
    tmp_path, setpoint, valid,
):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        charge_window_start="00:00", charge_window_end="06:00")
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "-500", "attributes": {}},
    )
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": setpoint, "attributes": {}},
    )
    await record_guard(settings, START, revision(), "observe")
    (row,) = await ReadinessLedger(settings.db_path).guards()
    assert row.sample_valid is valid
    assert row.measured_import_w == -500
    if not valid:
        assert row.setpoint_w is None
        assert row.throttle_w is None
        assert row.breach is None
    else:
        assert row.throttle_w is not None
        assert row.breach is False
    assert all(c.request.method == "GET" for c in respx.calls)


@pytest.mark.parametrize("mode,hour", [("observe", 12), ("protect", 0), ("optimise", 0),
                                      ("authority_unknown", 0)])
@respx.mock
async def test_guard_does_not_record_outside_observe_window(tmp_path, mode, hour):
    settings = Settings(ha_url="http://ha.test", ha_token="t",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid",
                        charge_window_start="00:00", charge_window_end="06:00")
    await record_guard(settings, START + timedelta(hours=hour), revision(), mode)
    assert await ReadinessLedger(settings.db_path).guards() == []
    assert len(respx.calls) == 0
