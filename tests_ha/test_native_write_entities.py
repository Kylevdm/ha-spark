"""Owner entity writes use the real clamped boundary and fake Solis registers."""
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import ManualSession, Transition
from custom_components.ha_spark.commands import IntentResult
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.number import NativeCurrent
from custom_components.ha_spark.select import NativePower
from custom_components.ha_spark.solis_transport import ReadResult
from custom_components.ha_spark.time import NativeTime
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
async def writer(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                 native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
                 freezer: FrozenDateTimeFactory) -> SparkCoordinator:
    from custom_components.ha_spark import native_write

    async def sleep(seconds: float) -> None:
        pass

    freezer.move_to("2026-11-02T12:00:00Z")
    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    entry = await native_entry(hass, aioclient_mock)
    coordinator: SparkCoordinator = entry.runtime_data
    # #580 supplies the startup reconciliation writer; model its completion.
    coordinator.control.ready = True
    monkeypatch.setattr(coordinator.soc, "increases_allowed", lambda bank: True)
    assert coordinator.guard is not None
    monkeypatch.setattr(coordinator.guard, "allowance_for", lambda bank: None)
    native_transport.set_input(33132, [32])
    native_transport.set_holding(43141, [100, 100] + [0] * 28)
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.set_holding(43007, [190])
    from custom_components.ha_spark.solis_frames import Reading
    inverter_id = next(iter(coordinator.native.inverters))
    coordinator.native.readings.setdefault(inverter_id, {})["holding_43007"] = Reading(
        datetime.now(UTC), (190,), 0)
    coordinator.native.readings[inverter_id]["holding_43141"] = Reading(
        datetime.now(UTC), tuple(native_transport.holding[a] for a in range(43141, 43171)), 0)
    native_transport.traffic.clear()
    return coordinator


async def mode(writer: SparkCoordinator, value: str, keep: bool = True) -> None:
    state = writer.authority.state
    assert state is not None
    updates: dict[str, Any] = {"committed_mode": value if value != "manual" else "optimise"}
    if value == "manual":
        updates["manual_session"] = ManualSession(
            deadline=None, return_to="optimise", keep_protection=keep)
    assert await writer.authority.async_save(state.model_copy(update=updates))


@pytest.mark.parametrize(("value", "keep", "charge", "discharge", "power"), [
    ("observe", True, True, False, True),
    ("protect", True, True, False, False),
    ("optimise", True, False, False, False),
    ("manual", True, True, True, False),
    ("manual", False, True, True, True),
])
async def test_entity_gate_every_mode(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, value: str, keep: bool,
    charge: bool, discharge: bool, power: bool,
) -> None:
    await mode(writer, value, keep)
    inverter = next(iter(writer.native.inverters.values()))
    seam = AsyncMock(return_value=IntentResult(outcome="unchanged"))
    monkeypatch.setattr("custom_components.ha_spark.native_power_seam.async_set_power_switch", seam)
    for entity, allowed, method, argument in [
        (NativeCurrent(writer, inverter, False), charge, "async_set_native_value", 15),
        (NativeCurrent(writer, inverter, True), discharge, "async_set_native_value", 15),
        (NativeTime(writer, inverter, 0), charge, "async_set_value", time(23)),
        (NativeTime(writer, inverter, 4), discharge, "async_set_value", time(22)),
        (NativePower(writer, inverter), power, "async_select_option", "off"),
    ]:
        native_transport.traffic.clear()
        if allowed:
            await getattr(entity, method)(argument)
        else:
            with pytest.raises(ServiceValidationError):
                await getattr(entity, method)(argument)
            assert not [t for t in native_transport.traffic if t.op == "write"]
    assert seam.await_count == int(power)


@pytest.mark.parametrize("fence", ["unknown", "save_failed", "pending", "read_only",
                                     "transition", "fallback", "startup"])
async def test_entity_fences(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fence: str,
) -> None:
    await mode(writer, "observe")
    state = writer.authority.state
    assert state is not None
    if fence in ("unknown", "save_failed"):
        monkeypatch.setattr(writer.authority, "status", fence)
    elif fence == "pending":
        monkeypatch.setattr(writer.control, "owned", lambda: False)
    elif fence == "read_only":
        monkeypatch.setattr(writer.native, "reason", lambda inv: "caching_on")
    elif fence == "startup":
        monkeypatch.setattr(writer.control, "ready", False)
    else:
        updates = ({"transition": Transition(target="optimise", started_at=datetime.now(UTC),
                                            steps=())} if fence == "transition" else
                   {"fallback": state.fallback.model_copy(update={"active": True})})
        await writer.authority.async_save(state.model_copy(update=updates))
    inverter = next(iter(writer.native.inverters.values()))
    for entity, method, argument in [
        (NativeCurrent(writer, inverter, False), "async_set_native_value", 20),
        (NativeCurrent(writer, inverter, True), "async_set_native_value", 20),
        (NativeTime(writer, inverter, 0), "async_set_value", time(23)),
        (NativeTime(writer, inverter, 6), "async_set_value", time(1)),
        (NativePower(writer, inverter), "async_select_option", "off"),
    ]:
        with pytest.raises(ServiceValidationError):
            await getattr(entity, method)(argument)
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_observe_clamps_and_logs_caller(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    await mode(writer, "observe")
    assert writer.guard is not None
    monkeypatch.setattr(writer.guard, "allowance_for", lambda bank: 0.5)
    inverter = next(iter(writer.native.inverters.values()))
    entity = NativeCurrent(writer, inverter, False)
    entity.async_set_context(Context(user_id="opaque-user"))
    caplog.set_level("INFO")
    await entity.async_set_native_value(30)
    assert 0 < native_transport.holding[43141] < 300
    assert writer.guard.last_requested_a[inverter.id] == 30
    assert "opaque-user" in caplog.text
    assert "gateway.invalid" not in caplog.text


async def test_time_whole_block_and_invalid_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    await mode(writer, "observe")
    inverter = next(iter(writer.native.inverters.values()))
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    entity = NativeTime(writer, inverter, 0)
    await entity.async_set_value(time(22, 15))
    assert [native_transport.holding[a] for a in range(43143, 43151)] == [
        22, 15, 5, 30, 0, 0, 0, 0]
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert writes[-1].address == 43143
    for value in (time(5, 30), time(22, 15, 1)):
        with pytest.raises(ServiceValidationError):
            await entity.async_set_value(value)


async def test_entity_readback_failure(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    await mode(writer, "observe")
    inverter = next(iter(writer.native.inverters.values()))
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    with pytest.raises(HomeAssistantError):
        await NativeCurrent(writer, inverter, False).async_set_native_value(20)


async def test_power_adapter_is_no_write(writer: SparkCoordinator) -> None:
    from custom_components.ha_spark.native_power_seam import async_set_power_switch
    state = writer.authority.state
    assert state is not None
    result = await async_set_power_switch(writer, "inv", True, epoch=state.epoch)
    assert result.outcome == "refused" and result.reason == "not_available"


async def test_registered_config_entities_and_service_state(
    writer: SparkCoordinator, hass: HomeAssistant, native_transport: FakeSolisDevice,
    caplog: pytest.LogCaptureFixture, hass_admin_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await mode(writer, "observe")
    entries = [e for e in er.async_get(hass).entities.values()
               if e.config_entry_id == writer.entry.entry_id
               and e.domain in ("number", "time", "select")]
    assert len(entries) == 7
    assert all(e.entity_category == "config" for e in entries)
    writer.async_update_listeners()
    entity = next(e for e in entries if e.unique_id.endswith("holding_43141_set"))
    caplog.set_level("INFO")
    await hass.services.async_call("number", "set_value", {
        "entity_id": entity.entity_id, "value": 17.9,
    }, blocking=True, context=Context(user_id=hass_admin_user.id))
    state = hass.states.get(entity.entity_id)
    assert state is not None and state.state == "17.0"
    assert native_transport.holding[43141] == 170
    assert hass_admin_user.id in caplog.text
    # Read-only validation does not hide readable device state.
    monkeypatch.setattr(writer.native, "reason", lambda inverter: "caching_on")
    writer.async_update_listeners()
    assert hass.states.get(entity.entity_id) == state
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("number", "set_value", {
            "entity_id": entity.entity_id, "value": 18,
        }, blocking=True)


@pytest.mark.parametrize("offset", [0, 2, 4, 6])
async def test_each_time_edit_keeps_other_words(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, offset: int,
) -> None:
    await mode(writer, "manual", False)
    inverter = next(iter(writer.native.inverters.values()))
    resident = [23, 30, 5, 30, 20, 0, 21, 0]
    native_transport.set_holding(43143, resident)
    await NativeTime(writer, inverter, offset).async_set_value(time(19, 45))
    resident[offset:offset + 2] = [19, 45]
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert len(writes) == 1 and writes[0].address == 43143
    assert [native_transport.holding[a] for a in range(43143, 43151)] == resident


@pytest.mark.parametrize("revocation", ["epoch", "read_only", "limits"])
async def test_entity_rechecks_after_last_read(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, revocation: str,
) -> None:
    await mode(writer, "observe")
    original = native_transport.read_holding
    async def read(address: int, count: int, *, device_id: int = 1) -> ReadResult:
        result = await original(address, count, device_id=device_id)
        if address == 43141:
            if revocation == "epoch":
                state = writer.authority.state
                assert state is not None
                await writer.authority.async_save(state.model_copy(update={
                    "epoch": state.epoch + 1}))
            elif revocation == "read_only":
                monkeypatch.setattr(writer.native, "reason", lambda inv: "gateway_not_owned")
            else:
                assert writer.guard is not None
                monkeypatch.setattr(writer.guard, "_save_failed", True)
        return result
    monkeypatch.setattr(native_transport, "read_holding", read)
    inverter = next(iter(writer.native.inverters.values()))
    with pytest.raises(ServiceValidationError):
        await NativeCurrent(writer, inverter, False).async_set_native_value(20)
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert writer.guard is not None and inverter.id not in writer.guard.last_requested_a


async def test_charge_time_edit_writes_only_the_slot_block(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    """Ruling Q9(a): a time edit never writes 43141, even with an odd resident current."""
    await mode(writer, "observe")
    native_transport.set_holding(43141, [105])
    inverter = next(iter(writer.native.inverters.values()))
    assert writer.guard is not None
    before = dict(writer.guard.last_requested_a)
    await NativeTime(writer, inverter, 0).async_set_value(time(22))
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert writes and {t.address for t in writes} == {43143}
    assert writer.guard.last_requested_a == before


@pytest.mark.parametrize("offset", [None, 0])
async def test_soc_blocks_increases_and_charge_time_edits(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, offset: int | None,
) -> None:
    await mode(writer, "observe")
    monkeypatch.setattr(writer.soc, "increases_allowed", lambda bank: False)
    inverter = next(iter(writer.native.inverters.values()))
    with pytest.raises(ServiceValidationError):
        if offset is None:
            await NativeCurrent(writer, inverter, False).async_set_native_value(20)
        else:
            await NativeTime(writer, inverter, offset).async_set_value(time(22))
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_owner_zero_current_is_confirmed_six_times(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    await mode(writer, "observe")
    inverter = next(iter(writer.native.inverters.values()))
    await NativeCurrent(writer, inverter, False).async_set_native_value(0)
    assert sum(t.op == "read" and t.address == 43141
               for t in native_transport.traffic) == 7
    assert native_transport.holding[43141] == 0


@pytest.mark.parametrize("kind", ["charge", "time", "discharge"])
async def test_permission_and_soc_rules(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    await mode(writer, "manual", False)
    inverter = next(iter(writer.native.inverters.values()))
    native_transport.set_input(33132, [0])
    monkeypatch.setattr(writer.soc, "increases_allowed", lambda bank: False)
    if kind == "discharge":
        await NativeCurrent(writer, inverter, True).async_set_native_value(20)
        assert native_transport.holding[43142] == 200
    else:
        with pytest.raises(ServiceValidationError):
            if kind == "charge":
                await NativeCurrent(writer, inverter, False).async_set_native_value(5)
            else:
                await NativeTime(writer, inverter, 0).async_set_value(time(22))
        assert not [t for t in native_transport.traffic if t.op == "write"]
