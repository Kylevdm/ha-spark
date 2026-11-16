"""Every identifying field leaves the diagnostics download redacted."""

import json
import logging
from copy import deepcopy

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ha_spark.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.ha_spark.native import NativeDevices
from custom_components.ha_spark.solis_transport import TransportError
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_solcast import KEY, NOW, SITE
from tests_ha.test_solcast_flows import entry_for
from tests_ha.test_solis_transport import Client, Clock, make_transport
from tests_ha.test_topology_flow import STORED_TOPOLOGY

HOST = "canary-host-4d2c"
TOKEN = "canary-token-8e1f"
SESSION = "canary-session-33aa"
SITE_UUID = "canary-uuid-77cd"
CANARIES = (KEY, SITE, HOST, TOKEN, SESSION, SITE_UUID)


@pytest.fixture(autouse=True)
async def utc_zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture(autouse=True)
def freeze_diagnostics(freezer: FrozenDateTimeFactory) -> None:
    """A deterministic fetch age for the Solcast health block."""
    freezer.move_to(NOW)


async def keyed_entry(hass: HomeAssistant) -> MockConfigEntry:
    return entry_for(
        hass, host=HOST, auth_token=TOKEN, session_id=SESSION, site_id=SITE_UUID,
        solcast_api_key=KEY, solcast_resource_ids=[SITE],
    )


async def test_diagnostics_redacts_every_to_redact_field(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = await keyed_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    aioclient_mock.get(
        f"https://api.solcast.com.au/rooftop_sites/{SITE}/forecasts?format=json&hours=36",
        json={"forecasts": [{
            "period_end": "2026-10-27T12:30:00Z", "period": "PT30M",
            "pv_estimate10": 1, "pv_estimate": 2, "pv_estimate90": 3,
        }]},
    )
    await entry.runtime_data.solcast.async_refresh()
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    for field in TO_REDACT & set(entry.data):
        assert data["entry"][field] == "**REDACTED**"
    assert data["solcast"] == {
        "status": "ok", "calls_used": 1, "calls_limit": 10, "fetch_age_s": 0,
        "cached_periods": 1,
    }
    encoded = json.dumps(data)
    for canary in CANARIES:
        assert canary not in encoded
        assert canary not in caplog.text


async def test_diagnostics_without_a_key_has_no_solcast(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert data["solcast"] is None
    assert TOKEN not in json.dumps(data)


async def test_diagnostics_native_links_are_opaque_and_shared(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    topology = deepcopy(STORED_TOPOLOGY)
    topology["inverters"][0].update(transport="native", gateway_host="gateway.invalid")
    topology["inverters"].append({
        **topology["inverters"][0], "id": "inv2", "gateway_unit_id": 2,
    })
    native = NativeDevices(
        hass, MockConfigEntry(domain=entry.domain, options={"topology": topology}),
        owned=lambda: False, overlay=lambda: None, changed=lambda: None,
    )
    entry.runtime_data.native = native
    clock = Clock()
    client = Client(clock)
    transport = make_transport(client, clock)
    monkeypatch.setattr(
        "custom_components.ha_spark.native.transport_factory", lambda host, port: transport,
    )
    native.transports.clear()
    inverter = native.inverters["inv1"]
    assert native.transport(inverter) is transport
    assert native.transport(native.inverters["inv2"]) is transport
    gateway_id = native.gateway_id(inverter)
    assert transport.label == gateway_id[:8]
    assert "gateway.invalid" not in transport.label
    await transport._connect()
    client.connected = False
    transport._link_drop(TransportError.TIMEOUT, "input", 33000)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["native_links"] == {gateway_id: transport.link_stats()}
    assert data["native_links"][gateway_id]["drops_24h"] == 1
    encoded = json.dumps(data["native_links"])
    assert "gateway.invalid" not in encoded
    assert "host" not in encoded
    assert "port" not in encoded
    # A two-argument test transport without link_stats remains supported.
    assert inverter.gateway_host is not None
    native.transports[(inverter.gateway_host, inverter.gateway_port)] = native_transport
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["native_links"] == {}
    await transport.close()


async def test_diagnostics_native_links_none_without_runtime_or_native(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    entry = entry_for(hass)
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["native_links"] is None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert data["native_links"] is None
