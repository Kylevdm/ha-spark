"""Gateway devices and administrator-only reprobe, entirely against fake registers."""

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockUser
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import devices, native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def test_gateway_entities_and_reprobe_after_caching_off(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice,
) -> None:
    native_transport.caching = True
    entry = await native_entry(hass, aioclient_mock)
    inverter = entry.runtime_data.native.inverters["inv1"]
    gateway_id = entry.runtime_data.native.gateway_id(inverter)
    registry = er.async_get(hass)

    def value(platform: str, key: str) -> str:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{gateway_id}_{key}")
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        return state.state

    assert value("binary_sensor", "gateway_connected") == "on"
    assert value("binary_sensor", "gateway_claim") == "on"
    assert value("sensor", "gateway_caching") == "on"
    device_registry = dr.async_get(hass)
    gateway = device_registry.async_get_device_by_identifier((DOMAIN, gateway_id), entry.entry_id)
    site = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.data["site_id"]), entry.entry_id,
    )
    assert gateway is not None and site is not None
    assert gateway.via_device_id == site.id
    assert "gateway.invalid" not in str(gateway)
    native_transport.caching = False
    admin = MockUser(is_owner=True).add_to_hass(hass)
    before = len(native_transport.traffic)
    await hass.services.async_call(DOMAIN, "reprobe", {"inverter_id": "inv1"},
                                   blocking=True, context=Context(user_id=admin.id))
    await hass.async_block_till_done()
    assert len(native_transport.traffic) == before + 10
    assert (await devices(entry, aioclient_mock))[0]["validation"] == "validated"
    assert value("sensor", "gateway_caching") == "off"
    native_transport.connected = False
    entry.runtime_data.native.recheck()
    await hass.async_block_till_done()
    assert value("binary_sensor", "gateway_connected") == "off"


@pytest.mark.parametrize(("admin", "system"), [(False, False), (True, True)])
async def test_reprobe_refuses_non_owner_without_bus_traffic(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, admin: bool, system: bool,
) -> None:
    await native_entry(hass, aioclient_mock)
    user = MockUser(is_owner=admin, system_generated=system).add_to_hass(hass)
    before = len(native_transport.traffic)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reprobe", {"inverter_id": "inv1"},
                                       blocking=True, context=Context(user_id=user.id))
    assert len(native_transport.traffic) == before


async def test_mismatch_runs_one_probe_and_reload_probes_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    before = len(native_transport.traffic)
    await entry.runtime_data.native.async_readback_mismatch("inv1")
    assert len(native_transport.traffic) == before + 10
    native_transport.set_identity(model=0x27)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(native_transport.traffic) == before + 20
    assert (await devices(entry, aioclient_mock))[0]["reason"] == "unvalidated_identity"
