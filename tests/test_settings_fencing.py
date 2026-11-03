"""Settings acceptance fences work without moving authority or redirecting holds."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_spark.api.server import AppState
from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord, Transition
from ha_spark.config import ConfigError
from ha_spark.devices import inverter_device
from ha_spark.devices.execution import Ownership, WriteGate, execution_context
from ha_spark.energy.scheduler import notify_remapped_holds
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.settings_service import SettingsService
from tests.authority_helpers import seed_authority, seed_settings, settings_bytes
from tests.test_solis_power_switch import FakeRest, _intent, _now, _window

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


def app_state(tmp_path):
    service = seed_settings(tmp_path, {
        "solis_fallback_current_a": 20, "inverter_power_switch_entity": "select.old",
    })
    return AppState(service.settings, service.options_path, settings_service=service)


async def test_device_save_fences_previously_planned_action(tmp_path):
    state = app_state(tmp_path)
    context = execution_context()
    seed_authority(state.settings, "optimise")
    rest = FakeRest()
    device = inverter_device(state.settings, rest)
    state.apply_options({"inverter_power_switch_entity": "select.new"},
                        state.settings_service.revision, "seed")
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
    state.apply_options({"notify_service": "notify.phone"}, state.settings_service.revision, "seed")
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
    original = settings_bytes(tmp_path)
    settings = state.settings
    if mode in {"optimise", "unknown"}:
        with pytest.raises(ConfigError):
            state.apply_options(updates, state.settings_service.revision, "seed")
        assert settings_bytes(tmp_path) == original
        assert state.settings is settings
    else:
        state.apply_options(updates, state.settings_service.revision, "seed")
        assert settings_bytes(tmp_path) != original


def test_unknown_authority_still_saves_an_install_without_a_fallback(tmp_path):
    service = seed_settings(tmp_path, {"inverter_power_switch_entity": "select.old"})
    path = service.data_dir / "ha_spark_config.json"
    state = AppState(service.settings, service.options_path, settings_service=service)
    store = seed_authority(state.settings, "observe")
    store.store_path.unlink()
    state.apply_options({"notify_service": "notify.phone"}, state.settings_service.revision, "seed")
    assert json.loads(path.read_text())["settings"]["notify_service"] == "notify.phone"


async def test_remapped_hold_keeps_identity_and_blocks_new_mapping(tmp_path):
    state = app_state(tmp_path)
    store = seed_authority(state.settings, "protect")
    identity = HoldDevice(driver="solis", power_switch="select.old",
                          hub=state.settings.solis_control_hub,
                          slave=state.settings.solis_modbus_slave)
    record = HoldRecord(cause="ev", started_at=datetime.now(UTC), device=identity,
                        confirmed=True)
    store.save_holds((record,))
    state.apply_options({"inverter_power_switch_entity": "select.new"},
                        state.settings_service.revision, "seed")
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
            state.apply_options({"inverter_power_switch_entity": "select.new"},
                                state.settings_service.revision, "seed")
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
    original = settings_bytes(tmp_path)
    with pytest.raises(ConfigError, match="solis_fallback_current_a"):
        state.apply_options(
            {"solis_fallback_current_a": None}, state.settings_service.revision, "seed",
        )
    assert settings_bytes(tmp_path) == original


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
    state.apply_options({"inverter_power_switch_entity": "select.new"},
                        state.settings_service.revision, "seed")
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
    original = settings_bytes(tmp_path)
    if mode == "optimise":
        with pytest.raises(ConfigError, match="inverter configuration"):
            state.apply_options({"inverter": "alphaess"}, state.settings_service.revision, "seed")
        assert settings_bytes(tmp_path) == original
    else:
        snapshot = state.settings
        generation = execution_context().config_generation
        result = state.apply_options({"inverter": "alphaess"},
                                     state.settings_service.revision, "seed")
        assert result["status"] == "pending_restart"
        assert state.settings is snapshot
        assert execution_context().config_generation == generation
        state.settings_service = SettingsService(
            state.settings_service.options_path, settings=snapshot, data_dir=tmp_path,
        )
        state.settings_service.load(writable=True)
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
    snapshot = state.settings
    generation = execution_context().config_generation
    result = state.apply_options({"inverter": "alphaess"}, state.settings_service.revision, "seed")
    assert result["status"] == "pending_restart"
    assert state.settings is snapshot
    assert execution_context().config_generation == generation
    state.settings_service = SettingsService(
        state.settings_service.options_path, settings=snapshot, data_dir=tmp_path,
    )
    state.settings_service.load(writable=True)
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
