"""Native readings, entity metadata and polling against only the in-memory device."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.native_entities import BankSoC
from custom_components.ha_spark.solis_frames import POINTS
from custom_components.ha_spark.solis_transport import ReadResult
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def ticks(hass: HomeAssistant, freezer: FrozenDateTimeFactory, count: int) -> None:
    for _ in range(count):
        freezer.tick(timedelta(seconds=1))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()


async def test_native_energy_metadata_pacing_and_failure_isolation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33169, [1, 2])
    native_transport.set_input(33139, [73])
    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    inverter = native.inverters["inv1"]
    prefix = f"{native.gateway_id(inverter)}_1"
    registry = er.async_get(hass)

    def state(key: str) -> Any:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}_{key}")
        assert entity_id is not None
        result = hass.states.get(entity_id)
        assert result is not None
        return result

    before = len(native_transport.traffic)
    assert before == 10  # First poll is timer-driven, not part of setup/probing.
    await ticks(hass, freezer, 9)
    assert len(native_transport.traffic) == before + 9
    energy = state("input_33169")
    assert energy.state == "65538.0"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["device_class"] == "energy"
    assert energy.attributes["state_class"] == "total_increasing"
    assert state("input_33139").state == "73.0"
    assert BankSoC(entry.runtime_data, inverter.bank_id).native_value == 73
    assert not BankSoC(entry.runtime_data, "absent").available
    assert native.readings["inv1"]["input_33132"].read_at <= dt_util.utcnow()
    # A failed battery frame must not invalidate lifetime energy from another frame.
    native_transport.input.pop(33139)
    await ticks(hass, freezer, 10)
    assert state("input_33139").state == "unavailable"
    assert state("input_33169").state == "65538.0"
    native_transport.connected = False
    native.recheck()
    await hass.async_block_till_done()
    assert state("input_33169").state == "unavailable"
    assert all(t.op == "read" for t in native_transport.traffic)
    assert all(t.address != 33000 for t in native_transport.traffic[before:])
    assert await hass.config_entries.async_unload(entry.entry_id)
    count = len(native_transport.traffic)
    await ticks(hass, freezer, 3)
    assert len(native_transport.traffic) == count


async def test_probe_lock_blocks_poll_reads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    before = len(native_transport.traffic)
    async with native.lock:
        await ticks(hass, freezer, 2)
        assert len(native_transport.traffic) == before
    await ticks(hass, freezer, 1)
    assert len(native_transport.traffic) == before + 1
    assert native_transport.traffic[-1].address == 33132
    assert all(t.op == "read" for t in native_transport.traffic)


async def test_register_ids_defaults_translations_and_bank_ambiguity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [73])
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    native = entry.runtime_data.native
    inverter = native.inverters["inv1"]
    gateway = native.gateway_id(inverter)
    registry = er.async_get(hass)
    translations = await async_get_translations(hass, "en", "entity", [DOMAIN])
    primary = {
        "input_33029", "input_33161", "input_33165", "input_33169", "input_33173",
        "input_33177", "input_33057", "input_33130", "input_33133", "input_33135",
        "input_33139", "input_33149_signed", "input_33151", "input_33132_b5",
        "input_33022", "holding_43073_b4", "holding_43074",
    }
    assert {p.key for p in POINTS if p.primary} == primary
    for point in POINTS:
        platform = "binary_sensor" if point.kind.startswith("bit") else "sensor"
        uid = f"{gateway}_1_{point.key}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, uid)
        assert entity_id is not None
        registered = registry.async_get(entity_id)
        assert registered is not None
        assert (registered.disabled_by is None) == (point.key in primary)
        if point.key not in primary or point.kind == "clock":
            assert registered.entity_category == "diagnostic"
        key = "native_register" if point.name.startswith("Register ") else point.key
        assert translations[f"component.{DOMAIN}.entity.{platform}.{key}.name"]
        assert "gateway.invalid" not in uid and "inv1" not in uid
    for reserved in (43151, 43152, *range(33181, 33186)):
        assert all(p.address != reserved for p in POINTS)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{gateway}_1"), entry.entry_id)
    assert device is not None
    assert device.model == "0026/2031"
    assert device.serial_number is None
    gateway_device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, gateway), entry.entry_id)
    assert gateway_device is not None and device.via_device_id == gateway_device.id
    await ticks(hass, freezer, 18)
    assert not BankSoC(entry.runtime_data, inverter.bank_id).available
    # A reload starts with no cached frames, even if the old readings were good.
    assert native.readings["inv1"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert not entry.runtime_data.native.readings["inv1"]
    assert all(t.op == "read" for t in native_transport.traffic)


@pytest.mark.parametrize("words", [(0,), (0,) * 22, (True,) * 21, (-1,) * 21])
async def test_invalid_frame_never_becomes_a_reading(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    words: tuple[int, ...],
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    original = native_transport.read_input

    async def invalid(address: int, count: int, *, device_id: int = 1) -> ReadResult:
        if address == 33132:
            return ReadResult(registers=words)
        return await original(address, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", invalid)
    await native.async_poll(dt_util.utcnow())
    assert native.readings["inv1"]["input_33132"].registers is None
    assert not BankSoC(entry.runtime_data, "bank").available


async def test_unload_cancels_an_inflight_poll_before_closing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    started = asyncio.Event()
    gate = asyncio.Event()
    original = native_transport.read_input

    async def blocked(address: int, count: int, *, device_id: int = 1) -> ReadResult:
        started.set()
        await gate.wait()
        return await original(address, count, device_id=device_id)

    monkeypatch.setattr(native_transport, "read_input", blocked)
    task = hass.async_create_task(native.async_poll(dt_util.utcnow()))
    await started.wait()
    before = len(native_transport.traffic)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert task.cancelled()
    gate.set()
    await native.async_poll(dt_util.utcnow() + timedelta(seconds=1))
    assert len(native_transport.traffic) == before


async def test_readings_from_before_a_reconnect_are_not_shown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [73])
    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    inverter = native.inverters["inv1"]
    await native.async_poll(dt_util.utcnow())
    assert native.reading(inverter, "input_33132") is not None
    assert BankSoC(entry.runtime_data, inverter.bank_id).native_value == 73
    native_transport.connection_epoch = 1  # type: ignore[attr-defined]
    assert native.reading(inverter, "input_33132") is None
    assert BankSoC(entry.runtime_data, inverter.bank_id).native_value is None


async def test_unloading_stops_polling_before_close(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    native = entry.runtime_data.native
    before = len(native_transport.traffic)
    entry.runtime_data.async_stop()
    assert not native.closed
    await native.async_poll(dt_util.utcnow())
    assert len(native_transport.traffic) == before
    assert await hass.config_entries.async_unload(entry.entry_id)
