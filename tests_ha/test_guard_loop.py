"""HA report events feed the independent guard without hardware writes."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.clamp import DeviceIntent
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.guard_loop import SupplyGuardLoop
from custom_components.ha_spark.topology import MajorLoad, effective_limits
from custom_components.ha_spark.wire import parse_summary
from tests_ha.test_guard_clamp import FakeWriter, topology
from tests_ha.test_polling import URL, setup, summary


async def test_report_events_cut_and_stop(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    await loop.async_start()
    limit = effective_limits(site).connections[0].import_kw * 1000
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", str(limit + 1000), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    # Same state/attributes: HA emits state_reported, not state_changed.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.grid", str(limit + 1000), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert loop.states[grid.id].last_good_at is not None
    assert loop.states[grid.id].over_count == 2
    assert not fake.intents  # No collected allocation or authority mirror.
    await loop.async_stop()
    before = loop.states.copy()
    hass.states.async_set("sensor.grid", "0", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert loop.states == before


async def test_restart_keeps_cut_budget_and_unknown_epoch(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    first = SupplyGuardLoop(hass, entry, coordinator, site)
    await first.async_start()
    key = site.connections[0].id
    first.states[key] = replace(first.states[key], cut_kw=1, latched=True, restores_used=48,
                                restore_day=datetime.now(UTC).date(), epoch=7)
    await first.async_stop()
    second = SupplyGuardLoop(hass, entry, coordinator, site)
    await second.async_start()
    assert second.states[key].cut_kw == 1
    assert second.states[key].restores_left == 0
    assert second.states[key].restored
    await second.async_stop()


@pytest.mark.parametrize("mode,connected,expected", [
    ("optimise", True, True), ("protect", True, True),
    ("observe", True, False), ("optimise", False, False),
])
async def test_fake_writer_and_mirror(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mode: str,
    connected: bool, expected: bool,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"battery_power_entity": "sensor.inverter",
                                              "power_sign": "negative_import"})
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary,
                                  authority=replace(coordinator.summary.authority, mode=mode,
                                                    save_failed=False, transition_target=None))
    coordinator.connected = connected
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    limit = loop.limits.connections[0].import_kw * 1000
    hass.states.async_set("sensor.inverter", "-2000", {"unit_of_measurement": "W"})
    for _ in range(2):
        freezer.tick(timedelta(seconds=1))
        hass.states.async_set("sensor.grid", str(limit + 1000), {"unit_of_measurement": "W"})
        await hass.async_block_till_done()
    assert loop.limiting
    assert bool(fake.intents) == expected
    assert loop.decisions[grid.id].warn_reason == (None if expected else "authority_unavailable")
    if expected:
        assert fake.intents[-1].charge_a * site.banks[0].nominal_voltage_v <= 1000
    if expected:
        # Restore needs continuously fresh reports, not just a five-minute wait.
        for _ in range(11):
            freezer.tick(timedelta(seconds=30))
            hass.states.async_set("sensor.grid", "5000", {"unit_of_measurement": "W"})
            hass.states.async_set("sensor.inverter", "-2000", {"unit_of_measurement": "W"})
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
        assert not loop.limiting
        assert fake.intents[-1].charge_a == 40
        assert loop.states[grid.id].restores_used == 1
    await loop.async_stop()


async def test_corrupt_store_dormant_until_good_reading(hass: HomeAssistant) -> None:
    site = topology()
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    loop = SupplyGuardLoop(hass, entry, coordinator, site)
    await loop.store.async_save({site.connections[0].id: {"cut_kw": "invalid"}})
    await loop.async_start()
    state = loop.states[site.connections[0].id]
    assert not state.limiting and state.dormant_reason is not None
    assert not loop.last_requested_a
    await loop.async_stop()


async def test_loop_uses_summary_zone_else_ha_zone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    await hass.config.async_set_time_zone("America/New_York")
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    loop = SupplyGuardLoop(hass, entry, coordinator, topology())
    await loop.async_start()
    key = loop.topology.connections[0].id
    assert loop.states[key].restore_day.isoformat() == "2026-10-29"
    coordinator.summary = replace(parse_summary(summary()), household_timezone="Pacific/Auckland")
    await loop.async_evaluate()
    assert loop.states[key].restore_day.isoformat() == "2026-10-30"
    await loop.async_stop()


async def test_burst_reports_keep_individual_values(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    loop = SupplyGuardLoop(hass, entry, coordinator, site)
    await loop.async_start()
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    for watts in (20000, 10000, 21000):
        freezer.tick(timedelta(seconds=1))
        hass.states.async_set("sensor.grid", str(watts), {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert loop.states[grid.id].over_count == 1
    await loop.async_stop()


async def test_entry_without_topology_then_reload_cancels_old_loop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    assert entry.runtime_data.guard is None
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,)})
    hass.config_entries.async_update_entry(entry, options={"topology_revision": 1,
                                                          "topology": site.model_dump(mode="json")})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    old = entry.runtime_data.guard
    assert old is not None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    current = entry.runtime_data.guard
    assert current is not None and current is not old
    old_states = old.states.copy()
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", "10000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert old.states == old_states
    assert current.states[grid.id].last_good_at is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    before = current.states.copy()
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert current.states == before


@pytest.mark.parametrize("running", [False, True])
async def test_missing_timer_uses_reserve_excluding_running_loads(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, running: bool,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    load = MajorLoad(id="ev", connection_id=grid.id, kind="ev_charger", demand_kw=2,
                     power_entity="sensor.ev")
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,),
                                   "major_loads": (load,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    # The 50% missing-reading cut needs a controlled allocation, so a (fake) writer.
    loop = SupplyGuardLoop(hass, entry, SparkCoordinator(hass, entry, "0.0.0"), site,
                           writer=FakeWriter())
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", "17000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not loop.limiting  # stale, but still within grace from last report
    freezer.tick(timedelta(seconds=59))
    hass.states.async_set("sensor.ev", "200" if running else "0",
                          {"unit_of_measurement": "W"})
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert loop.limiting == (not running)
    if not running:
        assert loop.allowance_kw[grid.id] == 1
        assert loop.states[grid.id].latched
    await loop.async_stop()


async def test_failed_restore_save_keeps_ceiling(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    site = site.model_copy(update={"connections": (grid,), "inverters": (inv,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary, authority=replace(
        coordinator.summary.authority, mode="optimise", save_failed=False, transition_target=None,
    ))
    coordinator.connected = True
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    loop.last_requested_a[inv.id] = 40
    await loop.async_start()
    hass.states.async_set("sensor.inverter", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid", "5000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    now = datetime.now(UTC)
    loop.states[grid.id] = replace(loop.states[grid.id], cut_kw=1, latched=True,
                                   headroom_since=now - timedelta(minutes=5))
    with patch.object(loop.store, "async_save", new=AsyncMock(
        side_effect=RuntimeError("private-writer-canary"),
    )):
        await loop.async_evaluate()
    assert loop.allowance_kw[grid.id] == 1
    assert loop.states[grid.id].latched
    assert loop.decisions[grid.id].warn_reason == "guard_save_failed"
    assert not fake.intents
    await loop.async_stop()


@pytest.mark.parametrize("mode,connected,save_failed,expected", [
    ("optimise", True, False, True), ("observe", True, False, False),
    ("optimise", False, False, False), ("optimise", True, True, False),
])
async def test_apply_through_clamp_needs_permitting_mirror(
    hass: HomeAssistant, mode: str, connected: bool, save_failed: bool, expected: bool,
) -> None:
    site = topology()
    inv = site.inverters[0]
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary, authority=replace(
        coordinator.summary.authority, mode=mode, save_failed=save_failed,
        transition_target=None,
    ))
    coordinator.connected = connected
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    await loop.async_start()
    assert loop.writer is not None
    await loop.writer.async_write(DeviceIntent(inv.id, inv.bank_id, 40))
    assert bool(fake.intents) == expected
    await loop.async_stop()


async def test_connection_set_change_keeps_matching_cuts(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    """A topology edit that renames or drops a connection keeps the others' cuts."""
    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    key = site.connections[0].id
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    loop = SupplyGuardLoop(hass, entry, coordinator, site)
    stored = {"cut_kw": 1.5, "latched": True, "missing_since": None, "restores_used": 40,
              "restore_day": "2026-10-29", "epoch": None}
    await loop.store.async_save({key: stored, "removed_connection": stored})
    await loop.async_start()
    assert loop.states[key].cut_kw == 1.5
    assert loop.states[key].restores_used == 40
    assert set(loop.states) == {key}
    await loop.async_stop()
