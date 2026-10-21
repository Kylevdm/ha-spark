"""Settings acceptance fences work without moving authority or redirecting holds."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_spark.api.server import AppState
from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord, Transition
from ha_spark.config import ConfigError, validate_options_document
from ha_spark.devices import inverter_device
from ha_spark.devices.execution import Ownership, WriteGate, execution_context
from ha_spark.energy.scheduler import notify_remapped_holds
from ha_spark.ha.rest import HomeAssistantRest
from tests.authority_helpers import seed_authority
from tests.test_solis_power_switch import FakeRest, _intent, _now, _window


def app_state(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({
        "solis_fallback_current_a": 20, "inverter_power_switch_entity": "select.old",
    }))

    def reload():
        return validate_options_document(json.loads(path.read_text()))

    return AppState(reload(), path, reload=reload)


async def test_device_save_fences_previously_planned_action(tmp_path):
    state = app_state(tmp_path)
    context = execution_context()
    seed_authority(state.settings, "optimise")
    rest = FakeRest()
    device = inverter_device(state.settings, rest)
    state.apply_options({"solis_modbus_slave": 2})
    line = await device.set_charge_rate(1000)
    assert line.startswith("[REFUSED] stale config")
    assert not rest.calls
    with context.planning():
        line = await inverter_device(state.settings, rest).set_charge_rate(1000)
    assert not line.startswith("[REFUSED]")
    assert rest.calls


async def test_non_fencing_save_keeps_planned_action(tmp_path):
    state = app_state(tmp_path)
    seed_authority(state.settings, "optimise")
    rest = FakeRest()
    device = inverter_device(state.settings, rest)
    state.apply_options({"notify_service": "notify.phone"})
    line = await device.set_charge_rate(1000)
    assert not line.startswith("[REFUSED]")
    assert rest.calls


@pytest.mark.parametrize("updates", [
    {"solis_fallback_current_a": None},
    {"charge_window_end": "23:30"},
    {"max_charge_current_a": 10},
])
@pytest.mark.parametrize("mode", ["optimise", "protect", "observe", "unknown"])
def test_fallback_invalid_save_is_rejected_only_when_required(tmp_path, updates, mode):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "optimise" if mode == "unknown" else mode)
    if mode == "unknown":
        store.store_path.unlink()
    original = state.options_path.read_bytes()
    settings = state.settings
    if mode in {"optimise", "unknown"}:
        with pytest.raises(ConfigError):
            state.apply_options(updates)
        assert state.options_path.read_bytes() == original
        assert state.settings is settings
    else:
        state.apply_options(updates)
        assert state.options_path.read_bytes() != original


def test_unknown_authority_still_saves_an_install_without_a_fallback(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"inverter_power_switch_entity": "select.old"}))

    def reload():
        return validate_options_document(json.loads(path.read_text()))

    state = AppState(reload(), path, reload=reload)
    store = seed_authority(state.settings, "observe")
    store.store_path.unlink()
    state.apply_options({"notify_service": "notify.phone"})
    assert json.loads(path.read_text())["notify_service"] == "notify.phone"


async def test_remapped_hold_keeps_identity_and_blocks_new_mapping(tmp_path):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "protect")
    identity = HoldDevice(driver="solis", power_switch="select.old",
                          hub=state.settings.solis_control_hub,
                          slave=state.settings.solis_modbus_slave)
    record = HoldRecord(cause="ev", started_at=datetime.now(UTC), device=identity,
                        confirmed=True)
    store.save_holds((record,))
    state.apply_options({"inverter_power_switch_entity": "select.new"})
    rest = FakeRest()
    with execution_context().planning():
        line = await inverter_device(state.settings, rest).set_charge_rate(1000)
    assert line.startswith("[REFUSED] held identity remapped")
    with execution_context().planning():
        lines = await inverter_device(state.settings, rest).reconcile_holds(
            _intent(holds=(_window(-5),)), _now()
        )
    assert all(line.startswith("[REFUSED] held identity remapped") for line in lines)
    assert not rest.calls
    assert store.state.holds[0].device == identity
    assert store.state.holds[0].confirmed
    # Observe adds no remap refusal; it still simulates and never redirects release.
    store.save(store.state.model_copy(update={"committed_mode": "observe"}))
    with execution_context().planning():
        line = await inverter_device(state.settings, rest).set_charge_rate(1000)
    assert line.startswith("[SIMULATE]")
    assert not rest.calls


async def test_missing_planned_generation_refuses(tmp_path):
    state = app_state(tmp_path)
    seed_authority(state.settings, "optimise")
    execution_context().planned_generation.set(None)
    rest = FakeRest()
    line = await inverter_device(state.settings, rest).set_charge_rate(1000)
    assert line.startswith("[REFUSED] stale config")
    assert not rest.calls


async def test_save_during_prerequisite_read_fences_write(tmp_path):
    state = app_state(tmp_path)
    seed_authority(state.settings, "optimise")

    class SavingRest(FakeRest):
        async def get_state(self, entity_id):
            result = await super().get_state(entity_id)
            state.apply_options({"solis_modbus_slave": 2})
            return result

    rest = SavingRest()
    line = await inverter_device(state.settings, rest).set_charge_rate(1000)
    # A revoked in-flight sequence is reported as failed by the existing driver.
    assert line.startswith("[FAILED]")
    assert not rest.calls


def test_activating_optimise_also_validates_fallback(tmp_path):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "observe")
    store.save(store.state.model_copy(update={"transition": Transition(
        target="optimise", started_at=datetime.now(UTC),
    )}))
    original = state.options_path.read_bytes()
    with pytest.raises(ConfigError, match="solis_fallback_current_a"):
        state.apply_options({"solis_fallback_current_a": None})
    assert state.options_path.read_bytes() == original


@respx.mock
async def test_remap_notice_once_across_restart(tmp_path):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "protect")
    store.save_holds((HoldRecord(
        cause="ev", started_at=datetime.now(UTC), confirmed=True,
        device=HoldDevice(driver="solis", power_switch="select.old",
                          hub=state.settings.solis_control_hub,
                          slave=state.settings.solis_modbus_slave),
    ),))
    state.apply_options({"inverter_power_switch_entity": "select.new"})
    route = respx.post("http://ha.test/api/services/persistent_notification/create").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with HomeAssistantRest("http://ha.test/api", "test") as rest:
        await notify_remapped_holds(state.settings, rest)
        await notify_remapped_holds(state.settings, rest)
        # Reload from disk, using the original authority data directory.
        restarted = AuthorityStore(state.settings.model_copy(update={
            "ha_url": "http://ha.test", "ha_token": "test",
            "db_path": str(store.store_path.parent / "events.db"),
        }))
        assert restarted.state is not None
        execution_context().authority = restarted
        await notify_remapped_holds(state.settings, rest)
    assert route.call_count == 1
    message = json.loads(route.calls[0].request.content)["message"]
    assert "Observe" in message and "retained" in message
    assert restarted.state.holds[0].device.power_switch == "select.old"


@pytest.mark.parametrize("mode", ["optimise", "protect"])
def test_optimise_rejects_a_save_that_drops_the_fallback_inverter(tmp_path, mode):
    state = app_state(tmp_path)
    seed_authority(state.settings, mode)
    original = state.options_path.read_bytes()
    if mode == "optimise":
        with pytest.raises(ConfigError, match="inverter configuration"):
            state.apply_options({"inverter": "alphaess"})
        assert state.options_path.read_bytes() == original
    else:
        state.apply_options({"inverter": "alphaess"})
        assert state.settings.devices[0].driver == "alphaess"


@respx.mock
async def test_replacing_the_held_driver_counts_as_a_remap(tmp_path):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "protect")
    store.save_holds((HoldRecord(
        cause="ev", started_at=datetime.now(UTC), confirmed=True,
        device=HoldDevice(driver="solis", power_switch="select.old",
                          hub=state.settings.solis_control_hub,
                          slave=state.settings.solis_modbus_slave),
    ),))
    state.apply_options({"inverter": "alphaess"})
    gate = WriteGate(execution_context())
    with execution_context().planning():
        mode = gate.mode_for(Ownership.PROTECTION, state.settings.devices[0], state.settings)
    assert mode == "refuse:held identity remapped"
    route = respx.post("http://ha.test/api/services/persistent_notification/create").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with HomeAssistantRest("http://ha.test/api", "test") as rest:
        await notify_remapped_holds(state.settings, rest)
    assert route.call_count == 1
    assert store.state.holds[0].device.driver == "solis"
