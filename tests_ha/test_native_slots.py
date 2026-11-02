"""Legacy slot register effects through the native command boundary and fake bus."""
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_spark.commands import Command
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer

writer = current_writer


@pytest.fixture(autouse=True)
def slots(native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-11-02T12:00:00Z")
    for address in (43143, 43153, 43163):
        native_transport.set_holding(address, [0] * 8)


def window(c: SparkCoordinator, **changes: Any) -> Command:
    state = c.authority.state
    assert state is not None
    return Command(session_id="test", sequence=1, epoch=state.epoch, kind="apply", payload={
        "intents": [{"kind": "charge_window", "inverter_id": "inv1", "current_a": 20.9,
                     "start": "23:30", "end": "05:30", **changes}]})


def writes(fake: FakeSolisDevice) -> list[tuple[int, tuple[int, ...]]]:
    return [(t.address, t.payload) for t in fake.traffic
            if t.op == "write" and isinstance(t.payload, tuple)]


async def test_window_block_current_and_steady_state(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "unchanged"  # worst successful stage (unused slots already zero)
    assert result.intents[0].written_a == 20
    assert native_transport.traffic[0].address == 33132
    assert writes(native_transport) == [(43141, (200,)), (43143, (23, 30, 5, 30, 0, 0, 0, 0))]
    native_transport.traffic.clear()
    assert (await writer._async_dispatch_command(window(writer))).outcome == "unchanged"
    assert writes(native_transport) == []


@pytest.mark.parametrize("fault", [None, "silent_noop", "exception"])
async def test_live_window_deactivated_before_current(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, fault: str | None,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    if fault:
        native_transport.inject_write_fault(WriteFault(fault), address=43141)
    result = await writer._async_dispatch_command(window(writer))
    expected = [(43143, (0,) * 8), (43141, (200,))]
    if fault is None:
        expected.append((43143, (23, 30, 5, 30, 0, 0, 0, 0)))
    assert writes(native_transport) == expected
    outcomes = {None: "unchanged", "silent_noop": "unverified", "exception": "failed"}
    assert result.outcome == outcomes[fault]


async def test_unverified_deactivation_blocks_current(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    assert (await writer._async_dispatch_command(window(writer))).outcome == "unverified"
    assert writes(native_transport) == [(43143, (0,) * 8)]


@pytest.mark.parametrize("permission", [0, 3])
async def test_blocked_charge_still_cleans_discharge_and_unused_slots(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, permission: int,
) -> None:
    native_transport.set_input(33132, [permission])
    native_transport.set_holding(43143, [23, 30, 5, 30, 10, 0, 12, 0])
    native_transport.set_holding(43153, [1] * 8)
    result = await writer._async_dispatch_command(window(writer))
    assert (result.outcome, result.reason) == ("refused", "grid_charge_not_permitted")
    assert writes(native_transport) == [(43143, (23, 30, 5, 30, 0, 0, 0, 0)), (43153, (0,) * 8)]


@pytest.mark.parametrize("current", [0.0, 5.0, 20.0])
async def test_untrusted_soc_blocks_entire_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, current: float,
) -> None:
    monkeypatch.setattr(writer, "soc", SimpleNamespace(increases_allowed=lambda bank: False))
    native_transport.set_holding(43153, [1] * 8)
    result = await writer._async_dispatch_command(window(writer, current_a=current))
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert writes(native_transport) == []


async def test_export_none_keeps_charge_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 10, 0, 12, 0])
    command = window(writer)
    command.payload = {"intents": [{"kind": "export", "inverter_id": "inv1", "request": "none"}]}
    assert (await writer._async_dispatch_command(command)).outcome == "applied"
    assert writes(native_transport) == [(43143, (23, 30, 5, 30, 0, 0, 0, 0))]


@pytest.mark.parametrize("mode", ["activating", "observe", "protect", "manual", "unknown",
                                 "pending", "epoch", "read_only", "fallback"])
async def test_activation_clears_unknown_discharge_but_fences_never_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from datetime import UTC, datetime

    from custom_components.ha_spark.authority_store import ManualSession, Transition

    state = writer.authority.state
    assert state is not None
    request = window(writer)
    if mode == "activating":
        state = state.model_copy(update={"transition": Transition(
            target="optimise", started_at=datetime(2026, 11, 2, tzinfo=UTC), steps=())})
    elif mode in ("observe", "protect"):
        state = state.model_copy(update={"committed_mode": mode})
    elif mode == "manual":
        state = state.model_copy(update={"manual_session": ManualSession(
            deadline=None, return_to="optimise", keep_protection=True)})
    elif mode == "unknown":
        monkeypatch.setattr(writer.authority, "status", "unknown")
    elif mode == "pending":
        monkeypatch.setattr(writer.control, "owned", lambda: False)
    elif mode == "epoch":
        request = request.model_copy(update={"epoch": state.epoch + 1})
    elif mode == "read_only":
        monkeypatch.setattr(writer.native, "reason", lambda inv: "gateway_not_owned")
    elif mode == "fallback":
        state = state.model_copy(update={"fallback": state.fallback.model_copy(
            update={"active": True, "causes": ("addon_liveness",)})})
    if mode != "unknown":
        assert await writer.authority.async_save(state)
    native_transport.set_holding(43143, [0, 0, 0, 0, 10, 0, 12, 0])
    result = await writer._async_dispatch_command(request)
    if mode == "activating":
        assert result.outcome == "unchanged"
        blocks = [payload for address, payload in writes(native_transport) if address == 43143]
        assert blocks and all(block[4:] == (0,) * 4 for block in blocks)
    else:
        assert result.outcome == "refused"
        if mode in ("observe", "protect", "manual", "fallback"):
            assert result.reason == "mode"
        assert writes(native_transport) == []


@pytest.mark.parametrize("address", [43143, 43153, 43163])
@pytest.mark.parametrize("transient", [False, True])
async def test_zero_slot_prereads_require_sustained_confirmation(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    address: int, transient: bool,
) -> None:
    native_transport.set_input(33132, [0])  # cleanup only
    if transient:
        native_transport.set_holding(address, [1] * 8)
        native_transport.spurious_zeros.add(("holding", address, 8))
    result = await writer._async_dispatch_command(window(writer))
    assert result.reason == "grid_charge_not_permitted"
    blocks = [(a, p) for a, p in writes(native_transport) if a == address]
    if transient:
        # Slot 1's cleanup keeps the charge half it reads once the zero is disproved.
        expected = (1,) * 4 + (0,) * 4 if address == 43143 else (0,) * 8
        assert blocks == [(address, expected)]
    else:
        assert blocks == []
        reads = sum(t.op == "read" and t.address == address for t in native_transport.traffic)
        assert reads == 7


async def test_transient_slot_one_zero_still_deactivates_before_current(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.spurious_zeros.add(("holding", 43143, 8))
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "unchanged"
    assert writes(native_transport)[:2] == [(43143, (0,) * 8), (43141, (200,))]


async def test_transient_current_zero_preread_does_not_skip_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    from tests_ha.test_native_write import command

    native_transport.spurious_zeros.add(("holding", 43141, 1))
    result = await writer._async_dispatch_command(command(writer, 0))
    assert result.outcome == "applied"
    assert writes(native_transport) == [(43141, (0,))]


@pytest.mark.parametrize(("matches", "fault"), [(True, False), (True, True), (False, False)])
async def test_current_change_keeps_only_exact_planned_discharge(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, matches: bool, fault: bool,
) -> None:
    from custom_components.ha_spark.commands import ChargeWindow
    from custom_components.ha_spark.native_slots import NativeSlots

    discharge = [10, 0, 12, 0]
    native_transport.set_holding(43143, [23, 30, 5, 30, *discharge])
    if fault:
        native_transport.inject_write_fault(WriteFault("silent_noop"), address=43141)
    guard = writer.guard
    state = writer.authority.state
    assert guard is not None and state is not None
    request = ChargeWindow.model_validate(window(writer).payload["intents"][0])  # type: ignore[index]
    slots = NativeSlots(writer.writes, guard.topology.inverters[0], state.epoch, set(),
                        planned_discharge=discharge if matches else [11, 0, 12, 0])
    async with guard.lock:
        async with writer.native.lock:
            guard.last_requested_a[request.inverter_id] = request.current_a
            result, _ = await slots.window(guard, request)
    blocks = [p for a, p in writes(native_transport) if a == 43143]
    assert blocks[0] == (0, 0, 0, 0, *(discharge if matches else [0] * 4))
    if matches:
        assert all(block[4:] == tuple(discharge) for block in blocks)
    if fault:
        assert result.outcome == "unverified"
        assert blocks == [(0, 0, 0, 0, *discharge)]
    else:
        assert result.outcome == "unchanged"


@pytest.mark.parametrize("fault", ["exception", "timeout", "silent_noop"])
async def test_current_failure_never_activates_new_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, fault: str,
) -> None:
    native_transport.inject_write_fault(WriteFault(fault), address=43141)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == ("unverified" if fault == "silent_noop" else "failed")
    assert writes(native_transport) == [(43141, (200,))]


@pytest.mark.parametrize("fault", ["exception", "silent_noop"])
async def test_slot_failure_isolated_and_unused_slots_still_cleared(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, fault: str,
) -> None:
    native_transport.set_holding(43153, [1] * 8)
    native_transport.set_holding(43163, [1] * 8)
    native_transport.inject_write_fault(WriteFault(fault), address=43143)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == ("failed" if fault == "exception" else "unverified")
    assert writes(native_transport) == [
        (43141, (200,)), (43143, (23, 30, 5, 30, 0, 0, 0, 0)),
        (43153, (0,) * 8), (43163, (0,) * 8)]


async def test_unreadable_permission_still_cleans_up(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.input.pop(33132)
    native_transport.set_holding(43143, [23, 30, 5, 30, 10, 0, 12, 0])
    native_transport.set_holding(43153, [1] * 8)
    result = await writer._async_dispatch_command(window(writer))
    assert (result.outcome, result.reason) == ("refused", "grid_charge_not_permitted")
    assert writes(native_transport) == [(43143, (23, 30, 5, 30, 0, 0, 0, 0)), (43153, (0,) * 8)]


async def test_failed_blocked_cleanup_is_not_reported_as_just_refused(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_input(33132, [0])
    native_transport.set_holding(43143, [23, 30, 5, 30, 10, 0, 12, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    assert (await writer._async_dispatch_command(window(writer))).outcome == "unverified"


async def test_authority_rechecked_before_every_slot_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    original = native_transport.read_holding

    async def revoke(address: int, count: int, *, device_id: int = 1) -> Any:
        result = await original(address, count, device_id=device_id)
        if address == 43143:
            monkeypatch.setattr(writer.authority, "status", "save_failed")
        return result

    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.set_holding(43153, [1] * 8)
    monkeypatch.setattr(native_transport, "read_holding", AsyncMock(side_effect=revoke))
    result = await writer._async_dispatch_command(window(writer))
    assert (result.outcome, result.reason) == ("refused", "authority_unknown")
    assert writes(native_transport) == []


async def test_window_barrier_holds_both_locks_through_zero_confirmation(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from custom_components.ha_spark import native_write

    started, release = asyncio.Event(), asyncio.Event()

    async def sleep(seconds: float) -> None:
        assert writer.guard is not None
        assert writer.guard.lock.locked() and writer.native.lock.locked()
        started.set()
        await release.wait()

    monkeypatch.setattr(native_write, "verify_sleep", sleep)
    task = asyncio.create_task(writer._async_dispatch_command(window(writer)))
    await started.wait()
    barrier = asyncio.create_task(writer.writes.async_barrier())
    await asyncio.sleep(0)
    assert not barrier.done()
    release.set()
    assert (await task).outcome == "unchanged"
    await barrier


@pytest.mark.parametrize("field", [{"start": "24:00"}, {"end": "05:60"},
                                   {"start": "05:30"}, {"soc_exempt": True}])
async def test_invalid_window_never_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, field: dict[str, Any],
) -> None:
    result = await writer._async_dispatch_command(window(writer, **field))
    assert (result.outcome, result.reason) == ("refused", "invalid_intent")
    assert writes(native_transport) == []


async def test_stale_nondriven_slot_is_the_only_write_when_window_is_set(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43141, [200])
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.set_holding(43153, [1] * 8)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "unchanged"
    assert writes(native_transport) == [(43153, (0,) * 8)]


@pytest.mark.parametrize("address", [43141, 43143])
async def test_unreadable_current_or_slot_fails_without_writes(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, address: int,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    for register in range(address, address + (1 if address == 43141 else 8)):
        native_transport.holding.pop(register)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "failed"
    # An unknown current may only deactivate the live window, never write 43141.
    assert writes(native_transport) == ([(43143, (0,) * 8)] if address == 43141 else [])


async def test_clamped_current_write_failure_never_activates_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    from custom_components.ha_spark.topology import effective_limits

    guard = writer.guard
    assert guard is not None
    inv = guard.topology.inverters[0].model_copy(update={"charge_limit_a": 10.0})
    site = guard.topology.model_copy(update={"inverters": (inv,)})
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options, "topology": site.model_dump(mode="json"),
    })
    guard.topology = site
    guard.limits = effective_limits(site)
    writer.native.inverters[inv.id] = inv
    native_transport.set_holding(43141, [600])
    native_transport.inject_write_fault(WriteFault("exception"), address=43141)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "failed"
    assert writes(native_transport) == [(43141, (100,))]
    await writer.hass.async_block_till_done()


async def test_transient_zero_during_deactivation_readback_blocks_current(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    original = native_transport.write

    async def write(address: int, values: Any, *, device_id: int = 1) -> Any:
        result = await original(address, values, device_id=device_id)
        if address == 43143:
            native_transport.spurious_zeros.add(("holding", 43143, 8))
        return result

    monkeypatch.setattr(native_transport, "write", write)
    result = await writer._async_dispatch_command(window(writer))
    assert result.outcome == "unverified"
    assert writes(native_transport) == [(43143, (0,) * 8)]


async def test_missing_soc_monitor_blocks_entire_window(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(writer, "soc", None)
    native_transport.set_holding(43153, [1] * 8)
    result = await writer._async_dispatch_command(window(writer))
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert native_transport.traffic == []


async def test_verified_current_stays_counted_when_a_later_slot_write_is_refused(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = native_transport.write

    async def write(address: int, values: Any, *, device_id: int = 1) -> Any:
        result = await original(address, values, device_id=device_id)
        if address == 43141:
            monkeypatch.setattr(writer.authority, "status", "save_failed")
        return result

    monkeypatch.setattr(native_transport, "write", write)
    result = await writer._async_dispatch_command(window(writer))
    assert (result.outcome, result.reason) == ("refused", "authority_unknown")
    assert writes(native_transport) == [(43141, (200,))]
    assert writer.guard is not None
    # 20 A is resident on the bus: peers must still be clamped against it.
    assert writer.guard.last_requested_a["inv1"] == 20.9


async def test_transient_zero_cleanup_read_keeps_the_charge_half(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 10, 0, 12, 0])
    native_transport.spurious_zeros.add(("holding", 43143, 8))
    command = window(writer)
    command.payload = {"intents": [{"kind": "export", "inverter_id": "inv1", "request": "none"}]}
    assert (await writer._async_dispatch_command(command)).outcome == "applied"
    assert writes(native_transport) == [(43143, (23, 30, 5, 30, 0, 0, 0, 0))]


async def test_soc_lost_mid_intent_stops_every_later_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.set_holding(43153, [1] * 8)
    original = native_transport.read_input

    async def lose(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        monkeypatch.setattr(writer, "soc", SimpleNamespace(increases_allowed=lambda bank: False))
        return result

    monkeypatch.setattr(native_transport, "read_input", lose)
    # A reduction (10 A resident to 5 A) still needs trusted SoC inside a window.
    result = await writer._async_dispatch_command(window(writer, current_a=5.0))
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert writes(native_transport) == []


async def test_failed_deactivation_does_not_count_the_new_request(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    assert (await writer._async_dispatch_command(window(writer))).outcome == "unverified"
    assert writer.guard is not None
    assert "inv1" not in writer.guard.last_requested_a


async def test_unhashable_intent_kind_is_a_positional_refusal(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    command = window(writer)
    command.payload = {"intents": [{"kind": []}, command.payload["intents"][0]]}  # type: ignore[index]
    result = await writer._async_dispatch_command(command)
    assert [i.reason for i in result.intents] == ["not_available", None]


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_peer_rebalance_keeps_the_peer_schedule(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert writer.guard is not None
    second = FakeSolisDevice()
    second.set_identity()
    second.set_input(33132, [32])
    second.set_holding(43141, [0])
    for address in (43143, 43153, 43163):
        second.set_holding(address, [0] * 8)
    second.traffic = native_transport.traffic
    monkeypatch.setattr(writer.native, "transport", lambda inv: (
        native_transport if inv.gateway_unit_id == 1 else second))
    await writer.async_reprobe("inv2")
    # At 50 V and eta .949, this admits 40 whole DC amps in total.
    monkeypatch.setattr(writer.guard, "allowance_for", lambda bank: 2.01 / 0.949)
    request = window(writer, inverter_id="inv2", current_a=40.0)
    assert (await writer._async_dispatch_command(request)).outcome in ("applied", "unchanged")
    assert second.holding[43141] == 400
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(window(writer, current_a=40.0))
    assert result.outcome in ("applied", "unchanged")
    peer = [(t.address, t.payload) for t in native_transport.traffic
            if t.op == "write" and t.device_id == 2]
    assert peer == [(43141, (200,))]
    assert [second.holding[r] for r in range(43143, 43151)] == [23, 30, 5, 30, 0, 0, 0, 0]
