"""Guard visibility: site entities, diagnostics and the snapshot block (#322).

The entities follow the guard loop, not the add-on link: the guard runs while
the add-on is down. A guard change sends an immediate heartbeat carrying the
v3 block, and the diagnostics download holds the history and raw readings.
"""

import json
from dataclasses import replace
from datetime import timedelta

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.topology import Topology
from custom_components.ha_spark.wire import parse_summary
from tests_ha.test_guard_clamp import topology
from tests_ha.test_polling import TOKEN, URL, fixture, setup, summary

LIMITING = "binary_sensor.ha_spark_supply_guard_limiting"
ALLOWANCE = "sensor.ha_spark_charging_allowance"
RESTORES = "sensor.ha_spark_guard_restores_left_today"


@pytest.fixture(autouse=True)
def freeze_guard_clock(freezer: FrozenDateTimeFactory) -> None:
    """Freeze before HA issues the test client's auth token (its JWT `iat`)."""
    freezer.move_to("2026-10-29T12:00:00+00:00")


def guarded_site() -> Topology:
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    return site.model_copy(update={"connections": (grid,), "inverters": (inv,)})


async def guarded_entry(hass: HomeAssistant) -> MockConfigEntry:
    site = guarded_site()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=fixture("site_snapshot")["site_id"], data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": TOKEN, "session_id": fixture("plan_summary")["session_id"],
    }, options={"topology": site.model_dump(mode="json"), "topology_revision": 1})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


def state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def over_limit(hass: HomeAssistant, freezer: FrozenDateTimeFactory, watts: str) -> None:
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.grid", watts, {"unit_of_measurement": "W"})


async def drive_cut(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Two consecutive over-limit reports (18 kW supply, 17 kW other load)."""
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", "19000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    over_limit(hass, freezer, "19000")
    await hass.async_block_till_done()


async def drive_restore(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry,
) -> None:
    """A permitting authority mirror, then five minutes of fresh headroom."""
    coordinator = entry.runtime_data
    coordinator.summary = replace(parse_summary(summary()), authority=replace(
        parse_summary(summary()).authority, mode="optimise"))
    coordinator.connected = True
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        hass.states.async_set("sensor.grid", "5000", {"unit_of_measurement": "W"})
        hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


async def test_entities_follow_the_guard_without_the_addon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await guarded_entry(hass)
    guard = entry.runtime_data.guard
    assert guard is not None and guard.started
    # Available with the add-on unreachable: the guard runs on its own loop.
    assert state(hass, LIMITING) == "off"
    assert state(hass, ALLOWANCE) == "unknown"
    registry = er.async_get(hass)
    restores = registry.async_get(RESTORES)
    assert restores is not None and restores.disabled
    assert restores.unique_id == f"{entry.data['site_id']}_guard_restores_left_today"
    limiting = registry.async_get(LIMITING)
    assert limiting is not None and not limiting.disabled

    guard.last_requested_a["one"] = 40
    await drive_cut(hass, freezer)
    assert state(hass, LIMITING) == "on"
    assert state(hass, ALLOWANCE) == "1.0"
    await drive_restore(hass, freezer, entry)
    assert state(hass, LIMITING) == "off"
    assert state(hass, ALLOWANCE) == "unknown"

    # Availability follows the guard loop starting, never the add-on link.
    await guard.async_stop()
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    assert state(hass, LIMITING) == "unavailable"
    assert state(hass, ALLOWANCE) == "unavailable"


async def test_no_topology_registers_no_guard_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    site_id = entry.data["site_id"]
    registry = er.async_get(hass)
    for entity_id in (LIMITING, ALLOWANCE, RESTORES):
        assert hass.states.get(entity_id) is None
        assert registry.async_get(entity_id) is None
    for key in ("supply_guard_limiting", "charging_allowance", "guard_restores_left_today"):
        assert registry.async_get_entity_id("sensor", DOMAIN, f"{site_id}_{key}") is None
    assert registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{site_id}_supply_guard_limiting") is None


async def test_diagnostics_hold_history_and_raw_readings(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
    hass_client: ClientSessionGenerator,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await guarded_entry(hass)
    guard = entry.runtime_data.guard
    assert guard is not None
    guard.last_requested_a["one"] = 40
    await drive_cut(hass, freezer)
    await drive_restore(hass, freezer, entry)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    guard_data = data["supply_guard"]
    assert guard_data is not None
    events = [record["event"] for record in guard_data["history"]]
    assert "cut" in events and "restore" in events
    history = guard_data["history"]
    assert all(set(record) == {"at", "connection_id", "event", "allowance_kw"}
               for record in history)
    readings = guard_data["readings"]
    assert readings["sensor.grid"]["power_w"] == 5000.0
    assert readings["sensor.inverter"]["power_w"] == 2000.0
    assert isinstance(readings["sensor.grid"]["reported_at"], str)
    connection = guard_data["connections"][0]
    assert connection["id"] == "grid"
    assert connection["cut_kw"] is None and connection["latched"] is False
    assert connection["restores_left"] == 47
    encoded = json.dumps(data)
    assert TOKEN not in encoded
    assert entry.data["site_id"] not in encoded


async def test_guard_change_sends_an_immediate_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await guarded_entry(hass)
    calls = len(aioclient_mock.mock_calls)
    guard = entry.runtime_data.guard
    assert guard is not None
    guard.last_requested_a["one"] = 40
    await drive_cut(hass, freezer)
    # The 30 s poll clock never advanced: only the guard's own change sent this.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(aioclient_mock.mock_calls) == calls + 1
    body = aioclient_mock.mock_calls[-1][2]
    block = body["supply_guard"]
    assert block == {
        "limiting": True,
        "connections": [{"id": "grid", "allowance_kw": 1.0}],
        "restores_left": 48,
        "revision": 1,
    }
    assert isinstance(block["limiting"], bool)
    assert 0 <= block["restores_left"] <= 48
    assert [c["id"] for c in block["connections"]] == [c.id for c in guard.topology.connections]
