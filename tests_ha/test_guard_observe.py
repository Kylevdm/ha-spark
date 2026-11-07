"""Guard in Observe on an integration-owned site reports without latching (#560)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import Transition
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.guard_loop import SupplyGuardLoop
from custom_components.ha_spark.topology import GridConnection, Inverter, Topology
from tests_ha.test_guard_clamp import FakeWriter, topology
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_polling import TOKEN, URL, fixture

LIMITING = "binary_sensor.ha_spark_supply_guard_limiting"
ALLOWANCE = "sensor.ha_spark_charging_allowance"


def observe_site() -> tuple[Topology, GridConnection, Inverter]:
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    return site.model_copy(update={"connections": (grid,), "inverters": (inv,)}), grid, inv


async def saved_guard(hass: HomeAssistant, entry: MockConfigEntry) -> object | None:
    """Read the durable guard store back independently of the loop."""
    return await Store[dict[str, object]](
        hass, 1, f"{DOMAIN}.supply_guard.{entry.entry_id}",
    ).async_load()


async def observe_authority(
    coordinator: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified local Observe store on an integration-owned site."""
    await coordinator.authority.async_load()
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    assert state.committed_mode == "observe"
    monkeypatch.setattr(coordinator.control, "owned", lambda: True)


async def drive_over_import(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, watts: str,
) -> None:
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", watts, {"unit_of_measurement": "W"})
    await hass.async_block_till_done(wait_background_tasks=True)
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.grid", watts, {"unit_of_measurement": "W"})
    await hass.async_block_till_done(wait_background_tasks=True)


async def drive_headroom(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        hass.states.async_set("sensor.grid", "5000", {"unit_of_measurement": "W"})
        hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("with_writer", [True, False])
async def test_observe_reports_simulated_limit_without_latching_or_saving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
    with_writer: bool,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake if with_writer else None)
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    assert loop.simulated
    limit = loop.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert loop.limiting
    assert loop.states[grid.id].cut_kw is not None
    assert not loop.states[grid.id].latched
    assert loop.decisions[grid.id].warn_reason == "mode_observe"
    assert not fake.intents
    assert await saved_guard(hass, entry) is None
    await drive_headroom(hass, freezer)
    assert not loop.limiting
    assert loop.states[grid.id].cut_kw is None
    assert loop.simulated
    assert not fake.intents
    await loop.async_stop()
    assert await saved_guard(hass, entry) is None


async def test_observe_missing_readings_never_latch(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    limit = loop.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert loop.simulated and loop.limiting
    freezer.tick(timedelta(seconds=121))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not loop.states[grid.id].latched
    assert loop.states[grid.id].dormant_reason == "missing_reading"
    await loop.async_stop()
    assert await saved_guard(hass, entry) is None


async def test_leaving_observe_starts_with_no_latched_cut(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, _inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    await loop.async_start()
    limit = loop.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert loop.simulated and loop.limiting
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(
        state.model_copy(update={"committed_mode": "protect"}))
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", str(limit + 1000), {"unit_of_measurement": "W"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not loop.simulated
    assert loop.states[grid.id].cut_kw is None
    assert not loop.states[grid.id].latched
    assert not loop.limiting
    await loop.async_stop()


async def test_restart_in_observe_loads_nothing_simulated(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, _inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    first = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    await first.async_start()
    limit = first.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert first.simulated and first.limiting
    await first.async_stop()
    assert await saved_guard(hass, entry) is None
    second = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    await second.async_start()
    assert second.simulated
    assert not second.limiting
    assert second.states[grid.id].cut_kw is None
    await second.async_stop()
    assert await saved_guard(hass, entry) is None


async def test_pending_observe_transition_keeps_protect_machine(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await coordinator.authority.async_load()
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    transition = Transition(target="observe", started_at=datetime.now(UTC))
    assert await coordinator.authority.async_save(state.model_copy(
        update={"committed_mode": "protect", "transition": transition}))
    monkeypatch.setattr(coordinator.control, "owned", lambda: True)
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    assert not loop.simulated
    limit = loop.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert loop.limiting
    assert not loop.states[grid.id].latched
    assert loop.decisions[grid.id].warn_reason == "mode_observe"
    assert not fake.intents
    await loop.async_stop()


async def test_entities_mark_simulated_only_in_observe(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    site, _grid, _inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=fixture("site_snapshot")["site_id"], data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": TOKEN, "session_id": fixture("plan_summary")["session_id"],
    }, options={"topology": site.model_dump(mode="json"), "topology_revision": 1})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    guard = coordinator.guard
    assert guard is not None and guard.started
    await observe_authority(coordinator, monkeypatch)
    limit = guard.limits.connections[0].import_kw * 1000
    await drive_over_import(hass, freezer, str(limit + 1000))
    assert guard.simulated
    limiting = hass.states.get(LIMITING)
    assert limiting is not None and limiting.state == "on"
    assert limiting.attributes.get("simulated") is True
    allowance = hass.states.get(ALLOWANCE)
    assert allowance is not None and allowance.attributes.get("simulated") is True
    connection = guard.diagnostics()["connections"][0]
    assert isinstance(connection, dict) and connection["simulated"] is True
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(
        state.model_copy(update={"committed_mode": "protect"}))
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", str(limit + 1000), {"unit_of_measurement": "W"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not guard.simulated
    limiting = hass.states.get(LIMITING)
    assert limiting is not None and limiting.state == ("on" if guard.limiting else "off")
    assert "simulated" not in limiting.attributes
    allowance = hass.states.get(ALLOWANCE)
    assert allowance is not None and "simulated" not in allowance.attributes
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stop_right_after_observe_commit_saves_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, grid, _inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(
        state.model_copy(update={"committed_mode": "protect"}))
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    await loop.async_start()
    assert not loop.simulated
    before = await saved_guard(hass, entry)
    # An unsaved in-memory Protect cut: a stop that saved would persist it.
    loop.states[grid.id] = replace(loop.states[grid.id], cut_kw=1, latched=True)
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(
        state.model_copy(update={"committed_mode": "observe"}))
    # No evaluate has seen the commit yet: the stop must still save nothing.
    await loop.async_stop()
    assert await saved_guard(hass, entry) == before


async def test_leaving_observe_refreshes_the_entities(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flip alone updates entities, so no stale `simulated` marker survives."""
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site, _grid, _inv = observe_site()
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    await observe_authority(coordinator, monkeypatch)
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    await loop.async_start()
    assert loop.simulated and not loop.limiting
    calls: list[None] = []
    monkeypatch.setattr(coordinator, "async_update_listeners", lambda: calls.append(None))
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(
        state.model_copy(update={"committed_mode": "protect"}))
    await loop.async_evaluate({})
    assert not loop.simulated and not loop.limiting
    assert calls
    await loop.async_stop()
