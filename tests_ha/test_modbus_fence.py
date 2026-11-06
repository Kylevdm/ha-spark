"""HA's loaded Modbus endpoints fence native writes, never reads."""
import asyncio
import importlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, get_args

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.commands import CommandResult, IntentResult, WriteReason
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.modbus_fence import hub_conflict
from custom_components.ha_spark.number import NativeCurrent
from custom_components.ha_spark.repairs import async_create_fix_flow
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_guard_writer import cut
from tests_ha.test_guard_writer import guarded as guarded_fixture
from tests_ha.test_native_clock import clock as clock_fixture
from tests_ha.test_native_clock import command as clock_command
from tests_ha.test_native_device import native_entry
from tests_ha.test_native_export import allow_export, apply_command, arm, window_intent
from tests_ha.test_native_fallback import fallback_site as fallback_fixture
from tests_ha.test_native_fallback import optimise as fallback_optimise
from tests_ha.test_native_holds import held_site as held_fixture
from tests_ha.test_native_holds import tick
from tests_ha.test_native_write import command, hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as writer_fixture
from tests_ha.test_native_write_entities import writer as entity_fixture

writer = writer_fixture
entity_writer = entity_fixture
held_site = held_fixture
clock = clock_fixture
fallback_site = fallback_fixture
guarded = guarded_fixture


@pytest.fixture(autouse=True)
def frozen_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-11-02T12:00:00Z")


@pytest.fixture
def modbus(hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    before = set(sys.modules)
    for name, attrs in {
        "modbus_connection": ("ModbusSerialParams", "ModbusTcpParams", "ModbusTlsParams",
                              "ModbusUdpParams", "ModbusUnit"),
        "modbus_connection.tmodbus": ("ModbusConnection",),
    }.items():
        module = ModuleType(name)
        for attr in attrs:
            setattr(module, attr, type(attr, (), {}))
        monkeypatch.setitem(sys.modules, name, module)
    const = importlib.import_module("homeassistant.components.modbus.const")
    connection = importlib.import_module("homeassistant.components.modbus.connection")
    hass.config.components.add("modbus")
    yield SimpleNamespace(const=const, connection=connection)
    hass.config.components.remove("modbus")
    for name in set(sys.modules) - before:
        if name.startswith(("homeassistant.components.modbus", "modbus_connection")):
            sys.modules.pop(name, None)


def install(c: SparkCoordinator, modbus: Any, source: str) -> None:
    endpoint = (("serial", "socket://gateway.invalid:502") if source == "rtu"
                else ("tcp", "gateway.invalid", 502))
    if source == "yaml":
        c.hass.data[modbus.const.DATA_MODBUS_HUBS] = {
            "hub": SimpleNamespace(endpoint=endpoint),
        }
    else:
        c.hass.data[modbus.connection.DATA_MODBUS_CONNECTIONS] = {
            endpoint: SimpleNamespace(connection=SimpleNamespace(connected=True),
                                      units={}, transient=0),
        }


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_matching_hub_refuses_charge_at_admission(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any, source: str,
) -> None:
    install(writer, modbus, source)
    result = await writer._async_dispatch_command(command(writer))
    assert (result.outcome, result.reason) == ("refused", "modbus_hub_conflict")
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_owner_entity_refused(
    entity_writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any, source: str,
) -> None:
    install(entity_writer, modbus, source)
    inverter = next(iter(entity_writer.native.inverters.values()))
    with pytest.raises(ServiceValidationError) as exc:
        await NativeCurrent(entity_writer, inverter, False).async_set_native_value(20)
    assert exc.value.translation_key == "modbus_hub_conflict"
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
@pytest.mark.parametrize("release", [False, True])
async def test_hold_start_and_restore_refused(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    source: str, release: bool,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    if release:
        await tick(c)
        assert c.authority.state is not None and c.authority.state.holds[0].confirmed
        c.hass.states.async_set("sensor.ev", "stopped")
    install(c, modbus, source)
    native_transport.traffic.clear()
    if release:
        assert not await c.holds.async_release(datetime.now(UTC), removal=True)
        assert c.authority.state is not None and c.authority.state.holds[0].confirmed
    else:
        await tick(c)
        assert native_transport.holding[43007] == 190
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
@pytest.mark.parametrize("repair", [False, True])
async def test_explicit_clock_and_repair_refused_in_observe(
    clock: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    source: str, repair: bool,
) -> None:
    install(clock, modbus, source)
    if repair:
        inverter = next(iter(clock.native.inverters.values()))
        flow = await async_create_fix_flow(clock.hass, clock.clock.issue_id(inverter), {
            "entry_id": clock.entry.entry_id, "inverter_id": inverter.id,
        })
        flow.hass = clock.hass
        result = await flow.async_step_confirm({})
        assert result["type"] == "form"
        assert result["errors"] == {"base": "modbus_hub_conflict"}
    else:
        result_command = await clock._async_dispatch_command(clock_command(clock))
        assert result_command.reason == "modbus_hub_conflict"
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_fallback_refused(
    fallback_site: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any, source: str,
) -> None:
    c = fallback_site
    await fallback_optimise(c)
    install(c, modbus, source)
    c.connected = False
    native_transport.traffic.clear()
    await c.fallback.async_evaluate(datetime.now(UTC))
    assert c.authority.state is not None and c.authority.state.fallback.active
    assert not c.fallback.in_force()
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_guard_cut_refused(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    source: str, freezer: FrozenDateTimeFactory,
) -> None:
    # A restore is written through the same guard_write admission as a cut.
    install(guarded, modbus, source)
    before = native_transport.holding[43141]
    native_transport.traffic.clear()
    await cut(guarded, freezer)
    assert native_transport.holding[43141] == before
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_export_window_refused(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    source: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    install(writer, modbus, source)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert result.reason == "modbus_hub_conflict"
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("endpoint", [
    ("tcp", "other.invalid", 502), ("tcp", "gateway.invalid", 503),
    ("serial", "/dev/ttyUSB0"),
])
async def test_other_endpoints_do_not_refuse(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any, endpoint: object,
) -> None:
    writer.hass.data[modbus.const.DATA_MODBUS_HUBS] = {"hub": SimpleNamespace(endpoint=endpoint)}
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"
    assert any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("source", ["yaml", "managed", "rtu"])
async def test_repair_clears_and_reads_continue(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    started = datetime.now(UTC)
    install(writer, modbus, source)
    inverter = next(iter(writer.native.inverters.values()))
    issue = ("ha_spark", writer.native.issue_id(inverter))
    await writer.native.async_check_connections()
    assert ir.async_get(writer.hass).issues[issue].translation_key == "native_modbus_hub_conflict"
    for _ in range(10):
        freezer.tick(timedelta(seconds=1))
        await writer.native.async_poll(datetime.now(UTC))
    assert any(t.op == "read" for t in native_transport.traffic)
    assert any(r.read_at > started for r in writer.native.readings[inverter.id].values())
    snapshot: dict[str, object] = {"api_version": 4}
    writer._ownership_snapshot(snapshot)
    assert snapshot["devices"] == [{"id": inverter.id, "transport": "native",
                                     "validation": "read_only", "capabilities": [],
                                     "reason": "gateway_not_owned"}]
    writer.hass.data[modbus.const.DATA_MODBUS_HUBS] = {}
    writer.hass.data[modbus.connection.DATA_MODBUS_CONNECTIONS] = {}
    await writer.native.async_check_connections()
    assert issue not in ir.async_get(writer.hass).issues
    assert (await writer._async_dispatch_command(command(writer))).outcome == "applied"


@pytest.mark.parametrize("failure", ["modules", "hub", "connections", "registry", "info"])
async def test_unreadable_fails_closed(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    if failure == "modules":
        monkeypatch.delitem(sys.modules, "homeassistant.components.modbus.connection")
    elif failure == "hub":
        writer.hass.data[modbus.const.DATA_MODBUS_HUBS] = {"hub": object()}
    elif failure == "registry":
        writer.hass.data[modbus.const.DATA_MODBUS_HUBS] = []
    elif failure == "info":
        monkeypatch.setattr(modbus.connection, "async_get_connection_info", lambda hass: {})
    else:
        def broken(hass: HomeAssistant) -> None:
            raise RuntimeError("untrusted detail")
        monkeypatch.setattr(modbus.connection, "async_get_connection_info", broken)
    writer.native.recheck()
    inverter = next(iter(writer.native.inverters.values()))
    issue = ("ha_spark", writer.native.issue_id(inverter))
    assert ir.async_get(writer.hass).issues[issue].translation_key == "native_modbus_hub_unreadable"
    result = await writer._async_dispatch_command(command(writer))
    assert result.reason == "modbus_hub_conflict"
    assert not any(t.op == "write" for t in native_transport.traffic)


@pytest.mark.parametrize("host,endpoint,expected", [
    ("Gateway.INVALID", ("tls", "GATEWAY.invalid", 502), "modbus_hub_conflict"),
    ("2001:db8::1", ("serial", "socket://[2001:DB8::1]:502"), "modbus_hub_conflict"),
    ("2001:db8::1", ("serial", "socket://[2001:db8::1]:503"), None),
    ("gateway.invalid", ("tcp", "gateway.invalid", True), "modbus_hub_unreadable"),
    ("gateway.invalid", ("tcp", "gateway.invalid"), "modbus_hub_unreadable"),
    ("gateway.invalid", ("serial", "socket://gateway.invalid:port"), "modbus_hub_unreadable"),
    ("gateway.invalid", ("tcp", "", -1), "modbus_hub_unreadable"),
    ("gateway.invalid", ("serial", "/dev/ttyUSB0"), None),
])
def test_endpoint_matching(hass: HomeAssistant, modbus: Any, host: str,
                           endpoint: object, expected: str | None) -> None:
    hass.data[modbus.const.DATA_MODBUS_HUBS] = {"hub": SimpleNamespace(endpoint=endpoint)}
    assert hub_conflict(hass, host, 502) == expected


def test_present_but_empty_registry_is_unreadable(hass: HomeAssistant, modbus: Any) -> None:
    hass.data[modbus.const.DATA_MODBUS_HUBS] = None
    assert hub_conflict(hass, "gateway.invalid", 502) == "modbus_hub_unreadable"


@pytest.mark.parametrize("source", ["yaml", "managed", "unreadable"])
async def test_power_switch_refused(
    entity_writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    from custom_components.ha_spark import native_power
    from custom_components.ha_spark.select import NativePower

    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(native_power, "verify_sleep", sleep)
    state = entity_writer.authority.state
    assert state is not None
    assert await entity_writer.authority.async_save(
        state.model_copy(update={"committed_mode": "observe"}))
    if source == "unreadable":
        entity_writer.hass.data[modbus.const.DATA_MODBUS_HUBS] = {"hub": object()}
    else:
        install(entity_writer, modbus, source)
    inverter = next(iter(entity_writer.native.inverters.values()))
    with pytest.raises(ServiceValidationError) as exc:
        await NativePower(entity_writer, inverter).async_select_option("off")
    assert exc.value.translation_key == "modbus_hub_conflict"
    assert native_transport.holding[43007] == 190
    assert not any(t.op == "write" for t in native_transport.traffic)


def test_conflict_wins_over_unreadable(hass: HomeAssistant, modbus: Any) -> None:
    hass.data[modbus.const.DATA_MODBUS_HUBS] = {
        "bad": object(), "match": SimpleNamespace(endpoint=("tcp", "gateway.invalid", 502)),
    }
    assert hub_conflict(hass, "gateway.invalid", 502) == "modbus_hub_conflict"


def test_unloaded_modbus_needs_no_modules(hass: HomeAssistant) -> None:
    assert hub_conflict(hass, "gateway.invalid", 502) is None


def test_installed_ha_modbus_surfaces() -> None:
    spec = importlib.util.find_spec("homeassistant.components.modbus")
    assert spec is not None and spec.submodule_search_locations
    root = Path(next(iter(spec.submodule_search_locations)))
    sources = {name: (root / name).read_text()
               for name in ("const.py", "connection.py", "modbus.py")}
    for filename, tokens in {
        "const.py": ('DOMAIN = "modbus"', "DATA_MODBUS_HUBS: HassKey[", "HassKey(DOMAIN)"),
        "connection.py": ("DATA_MODBUS_CONNECTIONS", 'f"{DOMAIN}_connections"',
                          "def async_get_connection_info(hass: HomeAssistant)",
                          "endpoint: ModbusEndpoint", "socket://"),
        "modbus.py": ("self.endpoint",),
    }.items():
        for token in tokens:
            assert token in sources[filename], f"HA Modbus surface changed: {filename}: {token}"


async def test_setup_detects_loaded_hub(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, modbus: Any,
) -> None:
    hass.data[modbus.const.DATA_MODBUS_HUBS] = {
        "hub": SimpleNamespace(endpoint=("tcp", "gateway.invalid", 502)),
    }
    entry = await native_entry(hass, aioclient_mock)
    c: SparkCoordinator = entry.runtime_data
    inverter = next(iter(c.native.inverters.values()))
    issue = ("ha_spark", c.native.issue_id(inverter))
    assert ir.async_get(hass).issues[issue].translation_key == "native_modbus_hub_conflict"
    assert any(t.op == "read" for t in native_transport.traffic)
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_hub_loaded_while_waiting_for_bus_refuses_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, modbus: Any,
) -> None:
    async with writer.native.lock:
        task = asyncio.create_task(writer._async_dispatch_command(command(writer)))
        await asyncio.sleep(0)
        assert not task.done()
        install(writer, modbus, "yaml")
    result = await task
    assert result.reason == "modbus_hub_conflict"
    assert not any(t.op == "write" for t in native_transport.traffic)


def test_write_reason_contract() -> None:
    fixture = Path(__file__).parents[1] / "tests/fixtures/contract/write_reasons.json"
    reasons = json.loads(fixture.read_text())
    assert sorted(get_args(WriteReason)) == reasons
    for reason in reasons:
        assert IntentResult(outcome="refused", reason=reason).reason == reason
        assert CommandResult(session_id="test", sequence=1, outcome="refused",
                             reason=reason).reason == reason
