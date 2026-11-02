"""Native guard ceilings use local authority and verified current writes, never slots."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import native_write
from custom_components.ha_spark.commands import ChargeCurrentLimit
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.guard_loop import SupplyGuardLoop
from custom_components.ha_spark.solis_frames import Reading
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha import test_native_device
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as writer_fixture
from tests_ha.test_topology_flow import STORED_TOPOLOGY

writer = writer_fixture


@pytest.fixture
async def guarded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory, request: pytest.FixtureRequest,
) -> SparkCoordinator:
    freezer.move_to("2026-10-29T12:00:00Z")
    topology = deepcopy(STORED_TOPOLOGY)
    topology["connections"][0]["grid_power_entity"] = "sensor.grid"
    topology["banks"][0]["one_way_charge_efficiency"] = getattr(request, "param", 0.949)
    monkeypatch.setattr(test_native_device, "STORED_TOPOLOGY", topology)

    async def sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    entry = await test_native_device.native_entry(hass, aioclient_mock)
    coordinator: SparkCoordinator = entry.runtime_data
    state = coordinator.authority.state
    assert state is not None
    assert await coordinator.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
    }))
    monkeypatch.setattr(coordinator.soc, "increases_allowed", lambda bank: True)
    native_transport.set_input(33132, [32])
    native_transport.set_holding(43141, [400])
    native_transport.traffic.clear()
    return coordinator


async def report(coordinator: SparkCoordinator, *, grid_w: float, inverter_w: int = 2000) -> None:
    """Feed successfully read native power and one fresh whole-site report."""
    guard = coordinator.guard
    assert guard is not None
    now = datetime.now(UTC)
    words = [0] * 21
    words[19:21] = [inverter_w >> 16, inverter_w & 0xFFFF]
    coordinator.native.readings["inv1"]["input_33132"] = Reading(now, tuple(words), 0)
    coordinator.hass.states.async_set("sensor.grid", str(grid_w), {"unit_of_measurement": "W"})
    await guard.async_evaluate({"sensor.grid": (grid_w, now)})
    # Finish the change-triggered heartbeat before advancing the frozen clock;
    # otherwise a counted reply's verified authority save can overlap the next read.
    await coordinator.hass.async_block_till_done(wait_background_tasks=True)


async def cut(coordinator: SparkCoordinator, freezer: FrozenDateTimeFactory) -> None:
    guard = coordinator.guard
    assert guard is not None
    limit_w = guard.limits.connections[0].import_kw * 1000
    for _ in range(2):
        freezer.tick(timedelta(seconds=1))
        await report(coordinator, grid_w=limit_w + 1000)


@pytest.mark.parametrize("mode", ["protect", "optimise", "manual", "fallback"])
async def test_guard_current_only(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory, mode: str,
) -> None:
    state = guarded.authority.state
    guard = guarded.guard
    assert state is not None and guard is not None
    if mode == "manual":
        result = await guarded.control.async_request(TransitionRequest(target="manual"),
            now=datetime.now(UTC), zone=ZoneInfo("Europe/London"))
        assert result.refusal is None
    else:
        assert await guarded.authority.async_save(state.model_copy(update={
            "committed_mode": "protect" if mode == "protect" else "optimise",
            "fallback": state.fallback.model_copy(update={"active": mode == "fallback"}),
        }))
    await cut(guarded, freezer)
    assert native_transport.holding[43141] == 180  # 1 kW AC * .949 / 50 V, rounded down
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")
    assert guard.last_requested_a == ({"inv1": 40} if mode == "manual" else {})


async def test_guard_survives_addon_loss(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    state = guarded.authority.state
    assert state is not None
    await guarded.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    guarded.connected = False
    await cut(guarded, freezer)
    assert native_transport.holding[43141] == 180


@pytest.mark.parametrize("guarded", [1.0, 0.949, 0.8], indirect=True)
async def test_native_efficiency_units(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    guard = guarded.guard
    assert guard is not None
    await cut(guarded, freezer)
    eta = guard.topology.banks[0].one_way_charge_efficiency
    amps = native_transport.holding[43141] / 10
    assert amps == int(1000 * eta / 50)
    assert amps * 50 / eta <= 1000


@pytest.mark.parametrize("resident", [10, 40])
@pytest.mark.parametrize("manual", [False, True])
async def test_same_epoch_replay_only_lowers_without_restore_target(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
    resident: int, manual: bool,
) -> None:
    if manual:
        await guarded.control.async_request(TransitionRequest(target="manual"),
            now=datetime.now(UTC), zone=ZoneInfo("Europe/London"))
    await cut(guarded, freezer)
    previous = guarded.guard
    assert previous is not None and previous.limiting
    await previous.async_stop()
    native_transport.set_holding(43141, [resident * 10])
    native_transport.traffic.clear()
    guard = SupplyGuardLoop(guarded.hass, guarded.entry, guarded, previous.topology)
    guarded.guard = guard
    await guard.async_start()
    await report(guarded, grid_w=1000)
    assert native_transport.holding[43141] == min(resident, 18) * 10
    assert not guard.last_requested_a
    # Five minutes of fresh headroom cannot restore without a NEW request.
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        await report(guarded, grid_w=1000)
    assert guard.limiting and not guard.last_requested_a
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")


@pytest.mark.parametrize("change", ["epoch", "connection", "limit", "membership"])
async def test_restart_drops_changed_cut(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
    change: str,
) -> None:
    await cut(guarded, freezer)
    previous = guarded.guard
    state = guarded.authority.state
    assert previous is not None and previous.limiting and state is not None
    await previous.async_stop()
    topology = previous.topology
    if change == "epoch":
        await guarded.authority.async_save(state.model_copy(update={"epoch": state.epoch + 1}))
    elif change == "membership":
        topology = topology.model_copy(update={"inverters": ()})
    else:
        connection = topology.connections[0].model_copy(update={
            "grid_power_entity": "sensor.other"} if change == "connection" else {
                "import_limit_a": 10})
        topology = topology.model_copy(update={"connections": (connection,)})
    native_transport.traffic.clear()
    guard = SupplyGuardLoop(guarded.hass, guarded.entry, guarded, topology)
    guarded.guard = guard
    await guard.async_start()
    assert not guard.limiting
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_epoch_bump_keeps_live_ceiling(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    """Only a restart drops an old-epoch cut; a live mode change keeps the ceiling."""
    guard = guarded.guard
    assert guard is not None
    await cut(guarded, freezer)
    state = guarded.authority.state
    assert state is not None
    assert await guarded.authority.async_save(state.model_copy(update={
        "committed_mode": "protect", "epoch": state.epoch + 1}))
    result = await guarded.writes.async_current(ChargeCurrentLimit(
        kind="charge_current_limit", inverter_id="inv1", current_a=40), epoch=state.epoch + 1)
    assert result.outcome == "refused"
    freezer.tick(timedelta(seconds=1))
    await report(guarded, grid_w=1000)
    assert guard.limiting
    assert native_transport.holding[43141] == 180


@pytest.mark.parametrize("owner_a", [12, 30])
async def test_manual_ceiling_tracks_owner_not_guard(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
    owner_a: int,
) -> None:
    guard = guarded.guard
    assert guard is not None
    await guarded.control.async_request(TransitionRequest(target="manual"),
        now=datetime.now(UTC), zone=ZoneInfo("Europe/London"))
    assert guard.last_requested_a == {"inv1": 40}
    await cut(guarded, freezer)
    assert guard.last_requested_a == {"inv1": 40}
    # A later native poll differing from our verified cut is an owner's latest edit.
    freezer.tick(timedelta(seconds=1))
    now = datetime.now(UTC)
    native_transport.set_holding(43141, [owner_a * 10])
    guarded.native.readings["inv1"]["holding_43141"] = Reading(
        now, (owner_a * 10,) + (0,) * 29, 0)
    await report(guarded, grid_w=1000)
    assert guard.last_requested_a == {"inv1": owner_a}
    assert native_transport.holding[43141] <= min(owner_a, 18) * 10
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        await report(guarded, grid_w=1000)
    assert native_transport.holding[43141] == owner_a * 10
    assert guard.last_requested_a == {"inv1": owner_a}


async def test_manual_ceiling_follows_fresh_resident_before_poll(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    """An owner edit the poll cache hasn't seen yet still caps the restore."""
    guard = guarded.guard
    assert guard is not None
    await guarded.control.async_request(TransitionRequest(target="manual"),
        now=datetime.now(UTC), zone=ZoneInfo("Europe/London"))
    await cut(guarded, freezer)
    assert native_transport.holding[43141] == 180
    native_transport.set_holding(43141, [120])
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        await report(guarded, grid_w=1000)
    assert guard.last_requested_a == {"inv1": 12}
    assert native_transport.holding[43141] == 120


async def test_refused_new_epoch_request_cannot_restore_old_target(
    guarded: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = guarded.guard
    state = guarded.authority.state
    assert guard is not None and state is not None
    result = await guarded.writes.async_current(ChargeCurrentLimit(
        kind="charge_current_limit", inverter_id="inv1", current_a=40), epoch=state.epoch)
    assert result.outcome == "unchanged"
    assert guard.last_requested_a == {"inv1": 40}
    await guarded.authority.async_save(state.model_copy(update={"epoch": state.epoch + 1}))
    monkeypatch.setattr(guarded.soc, "increases_allowed", lambda bank: False)
    result = await guarded.writes.async_current(ChargeCurrentLimit(
        kind="charge_current_limit", inverter_id="inv1", current_a=50), epoch=state.epoch + 1)
    assert result.outcome == "refused" and result.reason == "untrusted_soc"
    assert not guard.last_requested_a


async def test_refused_restore_retains_ceiling(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice,
    freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = guarded.guard
    state = guarded.authority.state
    assert guard is not None and state is not None
    await cut(guarded, freezer)
    result = await guarded.writes.async_current(ChargeCurrentLimit(
        kind="charge_current_limit", inverter_id="inv1", current_a=40), epoch=state.epoch)
    assert result.outcome == "unchanged"
    monkeypatch.setattr(guarded.soc, "increases_allowed", lambda bank: False)
    for _ in range(11):
        freezer.tick(timedelta(seconds=30))
        await report(guarded, grid_w=1000)
    assert guard.limiting
    assert native_transport.holding[43141] == 180
    monkeypatch.setattr(guarded.soc, "increases_allowed", lambda bank: True)
    freezer.tick(timedelta(seconds=1))
    limit_w = guard.limits.connections[0].import_kw * 1000
    await report(guarded, grid_w=limit_w - 100, inverter_w=900)
    assert guard.limiting
    assert native_transport.holding[43141] == 180


@pytest.mark.parametrize("fail_reduction", [False, True])
@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_guard_reclamps_peers_reductions_first(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fail_reduction: bool,
) -> None:
    guard = writer.guard
    state = writer.authority.state
    assert guard is not None and state is not None
    second = FakeSolisDevice()
    second.set_input(33132, [32])
    second.set_holding(43141, [0])
    second.traffic = native_transport.traffic
    native_transport.set_holding(43141, [400])
    monkeypatch.setattr(writer.native, "transport", lambda inv: (
        native_transport if inv.gateway_unit_id == 1 else second))
    await writer.async_reprobe("inv2")
    monkeypatch.setattr(guard, "allowance_for", lambda bank: 2.01 / 0.949)
    native_transport.traffic.clear()
    if fail_reduction:
        native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    async with guard.lock:
        result = await writer.writes.async_guard_locked(
            {"inv1": 40, "inv2": 40}, epoch=state.epoch)
    writes = [t for t in native_transport.traffic if t.op == "write"]
    assert [(t.device_id, t.payload) for t in writes] == (
        [(1, (200,))] if fail_reduction else [(1, (200,)), (2, (200,))])
    assert result.outcome == ("unverified" if fail_reduction else "applied")
    assert not guard.last_requested_a


@pytest.mark.parametrize("fence", ["observe", "unknown", "save_failed", "pending", "addon"])
async def test_guard_fences_never_write(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch, fence: str,
) -> None:
    state = guarded.authority.state
    assert state is not None
    if fence == "observe":
        await guarded.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    elif fence in ("unknown", "save_failed"):
        monkeypatch.setattr(guarded.authority, "status", fence)
    else:
        monkeypatch.setattr(guarded.control, "owned", lambda: False)
        assert guarded.summary is not None
        guarded.summary = replace(guarded.summary, authority=replace(
            guarded.summary.authority, mode="optimise", save_failed=False,
            transition_target=None))
        guarded.connected = True
    await cut(guarded, freezer)
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_snapshot_writer_v4_only(
    guarded: SparkCoordinator, aioclient_mock: AiohttpClientMocker,
) -> None:
    await guarded.async_refresh()
    assert aioclient_mock.mock_calls[-1][2]["supply_guard"]["connections"][0]["writer"] is True
    guarded.api_version = 3
    await guarded.async_refresh()
    assert "writer" not in aioclient_mock.mock_calls[-1][2]["supply_guard"]["connections"][0]


async def test_guard_lock_serialises_apply(
    guarded: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    guard = guarded.guard
    state = guarded.authority.state
    assert guard is not None and state is not None
    async with guard.lock:
        apply = asyncio.create_task(guarded.writes.async_current(ChargeCurrentLimit(
            kind="charge_current_limit", inverter_id="inv1", current_a=30), epoch=state.epoch))
        await asyncio.sleep(0)
        assert not [t for t in native_transport.traffic if t.op == "write"]
    result = await apply
    assert result.outcome == "applied"
