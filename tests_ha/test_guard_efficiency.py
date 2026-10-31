"""One-way losses narrow both guard boundaries on shared electrical inputs."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_spark.clamp import DeviceIntent, clamp_intent
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.guard_loop import SupplyGuardLoop
from custom_components.ha_spark.supply_guard import (
    AuthorityMirror,
    GuardInputs,
    GuardReading,
    GuardState,
    InverterMeasurement,
    step,
)
from custom_components.ha_spark.topology import Topology, effective_limits
from custom_components.ha_spark.wire import parse_summary
from tests_ha.test_guard_clamp import FakeWriter
from tests_ha.test_polling import summary


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8])
def test_guard_efficiency_agreement(eta: float) -> None:
    site = Topology.model_validate({
        "connections": [{"id": "grid", "supply_voltage_v": 240.0, "import_limit_a": 75.0}],
        "banks": [{"id": "bank", "connection_id": "grid", "usable_capacity_kwh": 10.0,
                   "nominal_voltage_v": 50.0, "min_soc": 10.0, "target_cap": 100.0,
                   "charge_limit_a": 62.5, "served_load_share_pct": 100.0,
                   "one_way_charge_efficiency": eta}],
        "inverters": [{"id": "inv", "bank_id": "bank", "driver_kind": "solis",
                       "charge_limit_a": 62.5, "confirmed": True}],
    })
    now = datetime(2026, 10, 29, tzinfo=UTC)
    state = GuardState(restore_day=now.date())
    for seconds in (0, 1):
        at = now + timedelta(seconds=seconds)
        state, _ = step(state, GuardInputs(
            limit_kw=18, requested_kw=3 / eta, reserve_kw=0,
            reading=GuardReading(20000, at),
            measurements=(InverterMeasurement("inv", 3000, at),),
            mirror=AuthorityMirror("optimise", 1, False, False, None),
        ), at)
    intent = clamp_intent(DeviceIntent("inv", "bank", 60), allowance_kw=state.cut_kw,
                          last_requested_a={"inv": 60}, topology=site,
                          limits=effective_limits(site))
    # Same electrical case pinned by tests/test_supply_guard_efficiency.py.
    daemon = 1000 * eta
    assert intent is not None
    assert intent.charge_a == int(daemon / 50)
    assert daemon <= 1000
    assert intent.charge_a * 50 / eta <= 1000


@pytest.mark.parametrize("efficiencies", [(1.0, 1.0), (0.8, 1.0), (0.949, 0.8)])
@pytest.mark.parametrize("allowance", [0.0, 0.5, 3.0, 100.0])
def test_command_shares_never_widen(
    efficiencies: tuple[float, float], allowance: float,
) -> None:
    path = Path(__file__).parents[1] / "tests/fixtures/limits/shared_connection.json"
    site = Topology.model_validate(json.loads(path.read_text())["topology"])
    baseline = site.model_copy(update={"banks": tuple(
        b.model_copy(update={"one_way_charge_efficiency": 1.0}) for b in site.banks
    )})
    site = site.model_copy(update={"banks": tuple(
        b.model_copy(update={"one_way_charge_efficiency": eta})
        for b, eta in zip(site.banks, efficiencies, strict=True)
    )})
    requests = {"one": 40.0, "two": 20.0}
    ac_w = 0.0
    for inv, bank in zip(site.inverters, site.banks, strict=True):
        intent = DeviceIntent(inv.id, bank.id, requests[inv.id])
        before = clamp_intent(intent, allowance_kw=allowance, last_requested_a=requests,
                              topology=baseline, limits=effective_limits(baseline))
        after = clamp_intent(intent, allowance_kw=allowance, last_requested_a=requests,
                             topology=site, limits=effective_limits(site))
        assert before is not None and after is not None
        assert after.charge_a <= before.charge_a
        if efficiencies == (1.0, 1.0):
            assert after == before
        ac_w += after.charge_a * bank.nominal_voltage_v / bank.one_way_charge_efficiency
    assert ac_w <= allowance * 1000


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8])
@pytest.mark.parametrize("battery_w", [2000.0, -2000.0])
async def test_battery_fallback_direction_only_narrows(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, eta: float, battery_w: float,
) -> None:
    from tests_ha.test_guard_clamp import topology

    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    bank = site.banks[0].model_copy(update={"one_way_charge_efficiency": eta})
    inv = site.inverters[0].model_copy(update={"battery_power_entity": "sensor.battery"})
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    site = site.model_copy(update={"banks": (bank,), "inverters": (inv,),
                                   "connections": (grid,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.connected = True
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary, authority=replace(
        coordinator.summary.authority, mode="optimise", save_failed=False,
        transition_target=None,
    ))
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    loop.last_requested_a[inv.id] = 60
    await loop.async_start()
    try:
        limit_w = loop.limits.connections[0].import_kw * 1000
        hass.states.async_set("sensor.battery", str(battery_w), {"unit_of_measurement": "W"})
        for _ in range(2):
            freezer.tick(timedelta(seconds=1))
            hass.states.async_set("sensor.grid", str(limit_w + 1000),
                                  {"unit_of_measurement": "W"})
            await hass.async_block_till_done()
        # Charging: converting DC to AC must not grant extra measured headroom.
        # Discharging: only eta of the DC output offsets AC site load.
        adjusted = battery_w / eta if battery_w >= 0 else battery_w * eta
        expected_kw = max(0, battery_w - abs(adjusted - battery_w) - 1000) / 1000
        assert loop.allowance_kw[grid.id] == expected_kw
        assert fake.intents[-1].charge_a == int(expected_kw * 1000 * eta / bank.nominal_voltage_v)
        assert fake.intents[-1].charge_a <= int(
            max(0, battery_w - 1000) / bank.nominal_voltage_v
        )
    finally:
        await loop.async_stop()


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8, 5e-324])
def test_missing_reading_ceiling_never_exceeds_lossless(eta: float) -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    state = GuardState(restore_day=now.date(), last_good_at=now - timedelta(seconds=120),
                       last_headroom_kw=0)
    state, decision = step(state, GuardInputs(
        limit_kw=18, requested_kw=3 / eta, lossless_requested_kw=3, reserve_kw=1,
        reading=GuardReading(None, None),
        measurements=(InverterMeasurement("inv", None, None),),
        mirror=AuthorityMirror("optimise", 1, False, False, None),
    ), now)
    assert state.cut_kw == 1.5
    assert decision.allowance_kw == 1.5
    assert decision.write


@pytest.mark.parametrize("requested", [float("inf"), 3.0])
def test_overflow_request_still_enforces_guard_cut(requested: float) -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    state = GuardState(restore_day=now.date())
    for seconds in (0, 1):
        at = now + timedelta(seconds=seconds)
        state, decision = step(state, GuardInputs(
            limit_kw=18, requested_kw=requested, lossless_requested_kw=3, reserve_kw=0,
            reading=GuardReading(20000, at),
            measurements=(InverterMeasurement("inv", 3000, at),),
            mirror=AuthorityMirror("optimise", 1, False, False, None),
        ), at)
    assert state.cut_kw == 1
    assert decision.write


def test_tiny_efficiency_clamp_fails_closed() -> None:
    from tests_ha.test_guard_clamp import topology

    site = topology()
    site = site.model_copy(update={"banks": (
        site.banks[0].model_copy(update={"one_way_charge_efficiency": 5e-324}),
    )})
    inv = site.inverters[0]
    result = clamp_intent(DeviceIntent(inv.id, inv.bank_id, 60), allowance_kw=1,
                          last_requested_a={inv.id: 60}, topology=site,
                          limits=effective_limits(site))
    assert result is not None and result.charge_a == 0


@pytest.mark.parametrize("eta", [1.0, 0.8])
def test_discharge_fallback_losses_block_restore_with_nonzero_headroom(eta: float) -> None:
    now = datetime(2026, 10, 29, tzinfo=UTC)
    state = GuardState(restore_day=now.date(), cut_kw=1,
                       headroom_since=now - timedelta(seconds=300))
    state, _ = step(state, GuardInputs(
        limit_kw=18, requested_kw=1.8 / eta, reserve_kw=0,
        reading=GuardReading(13400, now),
        measurements=(InverterMeasurement("inv", -2000 * eta, now, -2000),),
        mirror=AuthorityMirror("optimise", 1, False, False, None),
    ), now)
    # 2.6 kW lossless headroom; eta=.8 discounts discharge by 400 W.
    # The 2.25 kW AC request + 200 W fits only without that discount.
    assert (state.cut_kw is None) == (eta == 1)


@pytest.mark.parametrize("eta,restored", [(1.0, True), (0.8, False)])
async def test_discharge_measurement_losses_reach_restore_boundary(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, eta: float, restored: bool,
) -> None:
    from tests_ha.test_guard_clamp import topology

    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    bank = site.banks[0].model_copy(update={"one_way_charge_efficiency": eta})
    inv = site.inverters[0].model_copy(update={"battery_power_entity": "sensor.battery"})
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    site = site.model_copy(update={"banks": (bank,), "inverters": (inv,),
                                   "connections": (grid,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.connected = True
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary, authority=replace(
        coordinator.summary.authority, mode="optimise", save_failed=False,
        transition_target=None,
    ))
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=FakeWriter())
    loop.last_requested_a[inv.id] = 36  # 1.8 kW DC
    await loop.async_start()
    try:
        now = datetime.now(UTC)
        loop.states[grid.id] = replace(loop.states[grid.id], cut_kw=1,
                                      headroom_since=now - timedelta(seconds=300))
        limit_w = loop.limits.connections[0].import_kw * 1000
        await loop.async_evaluate({"sensor.grid": (limit_w - 4600, now),
                                   "sensor.battery": (-2000, now)})
        assert (not loop.limiting) == restored
    finally:
        await loop.async_stop()


@pytest.mark.parametrize("eta,restored", [(1.0, True), (0.949, False), (0.8, False)])
async def test_restore_uses_ac_request_and_deadband(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, eta: float, restored: bool,
) -> None:
    from tests_ha.test_guard_clamp import topology

    freezer.move_to("2026-10-29T12:00:00+00:00")
    site = topology()
    bank = site.banks[0].model_copy(update={"one_way_charge_efficiency": eta})
    inv = site.inverters[0].model_copy(update={"grid_port_power_entity": "sensor.inverter"})
    grid = site.connections[0].model_copy(update={"grid_power_entity": "sensor.grid"})
    site = site.model_copy(update={"banks": (bank,), "inverters": (inv,),
                                   "connections": (grid,)})
    entry = MockConfigEntry(domain="ha_spark", data={})
    entry.add_to_hass(hass)
    coordinator = SparkCoordinator(hass, entry, "0.0.0")
    coordinator.connected = True
    coordinator.summary = parse_summary(summary())
    coordinator.summary = replace(coordinator.summary, authority=replace(
        coordinator.summary.authority, mode="optimise", save_failed=False,
        transition_target=None,
    ))
    fake = FakeWriter()
    loop = SupplyGuardLoop(hass, entry, coordinator, site, writer=fake)
    loop.last_requested_a[inv.id] = 40  # 2 kW DC, needs 2/eta kW AC.
    await loop.async_start()
    loop.states[grid.id] = replace(loop.states[grid.id], cut_kw=1)
    try:
        limit_w = loop.limits.connections[0].import_kw * 1000
        # 2.25 kW AC available: lossless request + deadband fits, lossy doesn't.
        for _ in range(12):
            freezer.tick(timedelta(seconds=30))
            hass.states.async_set("sensor.inverter", "1000", {"unit_of_measurement": "W"})
            hass.states.async_set("sensor.grid", str(limit_w - 1250),
                                  {"unit_of_measurement": "W"})
            await hass.async_block_till_done()
        assert (not loop.limiting) == restored
        assert loop.states[grid.id].restores_used == int(restored)
    finally:
        await loop.async_stop()
