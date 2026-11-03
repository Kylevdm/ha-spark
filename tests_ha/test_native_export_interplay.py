"""Native export interplay: holds, cancels, fallback, restart and replays.

Every case runs through the real config entry, the one charge-write path and the
in-memory fake inverter. Nothing here is a write permission: each assertion is a
register effect, a store state or a fixed outcome code.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_spark.authority_store import HoldDevice, HoldRecord
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.solis_frames import Reading
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_export import (  # noqa: F401 - hass fixtures
    allow_export,
    apply_command,
    arm,
    block_writes,
    charge_intents,
    command,
    export_writes,
    transition_kinds,
    window_intent,
)
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer

writer = current_writer
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
ZONE = ZoneInfo("Europe/London")
FALLBACK = {"window_start": "23:30", "window_end": "05:30", "currents": {"inv1": 20}}


@pytest.fixture(autouse=True)
def export_clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


def full_soc(c: SparkCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SoC source complete enough for the startup preconditions too."""
    monkeypatch.setattr(c, "soc", SimpleNamespace(
        trusted=lambda bank: True, increases_allowed=lambda bank: True,
        fallback_requested=False, recovery_ready=False, all_trusted=True, recovered=True,
        bank=lambda bank: SimpleNamespace(trust="trusted", reason=None),
        snapshot=lambda: [], observe=lambda now: None))


def slot_registers(transport: FakeSolisDevice, address: int) -> list[int]:
    return [transport.holding.get(register, 0) for register in range(address, address + 8)]


def ready(c: SparkCoordinator) -> None:
    """Permit non-protection steps, as the startup grace would once it passed."""
    c.control.ready = True
    c.control.startup_complete = True
    c.control.grace_until = NOW - timedelta(seconds=1)


async def armed_window(c: SparkCoordinator, transport: FakeSolisDevice,
                       monkeypatch: pytest.MonkeyPatch) -> None:
    allow_export(c, monkeypatch, transport)
    await arm(c)
    result = await c._async_dispatch_command(apply_command(c, [window_intent()]))
    assert result.outcome in ("applied", "unchanged")


def hold_for(c: SparkCoordinator, inverter_id: str) -> HoldRecord:
    assert c.guard is not None
    inverter = next(i for i in c.guard.topology.inverters if i.id == inverter_id)
    return HoldRecord(cause="ev", started_at=NOW, device=HoldDevice(
        driver="solis_native", power_switch=inverter.id,
        hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
        slave=inverter.gateway_unit_id), confirmed=True)


# --- holds beat export (Q12) ---------------------------------------------------

async def test_a_hold_beats_export_and_the_hold_reconcile_never_writes_slot_one(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    assert slot_registers(native_transport, 43143) == [0, 0, 0, 0, 14, 0, 15, 0]
    # The EV reports charging: the hold turns the switch Off and leaves Slot 1 alone.
    writer.hass.states.async_set("sensor.ev", "charging")
    native_transport.traffic.clear()
    await writer.holds.async_reconcile(NOW)
    assert native_transport.holding[43007] == 222
    assert not [t for t in native_transport.traffic
                if t.op == "write" and t.address == 43143]
    # The next apply refuses at the hold step and clears the discharge half.
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert (result.outcome, result.reason) == ("refused", "hold_overlaps_export")
    assert block_writes(native_transport)[-1] == (0, 0, 0, 0, 0, 0, 0, 0)
    state = writer.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"
    assert state.export_records[-1].cleanup_verified is True


# --- transition and manual cancels (Q4) ---------------------------------------

@pytest.mark.parametrize("target", ["protect", "observe", "manual"])
async def test_leaving_optimise_cancels_with_zero_confirmed_slots(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    full_soc(writer, monkeypatch)
    ready(writer)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.set_holding(43153, [1] * 8)
    native_transport.set_holding(43163, [1] * 8)
    native_transport.traffic.clear()
    request = TransitionRequest(target=target, duration="until_resumed")  # type: ignore[arg-type]
    decision = await writer.control.async_request(request, now=NOW, zone=ZONE)
    assert decision.refusal is None
    writes = [(t.address, t.payload) for t in native_transport.traffic if t.op == "write"]
    # The transition cancel keeps the charge half, zeroes both unused slots and
    # verifies the discharge half first time and on five further reads.
    assert (43143, (23, 30, 5, 30, 0, 0, 0, 0)) in writes
    assert (43153, (0,) * 8) in writes and (43163, (0,) * 8) in writes
    written_index = next(i for i, t in enumerate(native_transport.traffic)
                         if t.op == "write" and t.address == 43143)
    assert len([t for t in native_transport.traffic[written_index:]
                if t.op == "read" and t.address == 43143]) >= 6
    state = writer.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"
    assert state.export_records[-1].cleanup_verified is True
    assert not state.export_supervisions


async def test_a_failed_transition_clear_stays_an_unverified_step(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    full_soc(writer, monkeypatch)
    ready(writer)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    decision = await writer.control.async_request(
        TransitionRequest(target="protect"), now=NOW, zone=ZONE)
    assert decision.refusal is None
    state = writer.authority.state
    assert state is not None
    assert state.transition is not None
    assert state.transition.steps[0] == "cancel_export"
    assert state.transition.failing_actions == ("cancel_export",)
    assert state.export_records[-1].state != "cleared"
    assert ("aborted", "unverified") in transition_kinds(writer)


async def test_a_none_request_carries_the_five_read_zero_confirm(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [{"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    assert result.outcome == "applied"
    written_index = next(i for i, t in enumerate(native_transport.traffic)
                         if t.op == "write" and t.address == 43143)
    assert len([t for t in native_transport.traffic[written_index:]
                if t.op == "read" and t.address == 43143]) >= 6
    state = writer.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"


async def test_end_supervision_keeps_the_half_only_clear(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    supervision_id = str(state.export_supervisions[0].id)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.set_holding(43153, [1] * 8)
    native_transport.traffic.clear()
    ended = await writer._async_dispatch_command(command(
        writer, kind="end_supervision", sequence=2, supervision_id=supervision_id))
    assert ended.outcome == "applied"
    writes = [t.address for t in native_transport.traffic if t.op == "write"]
    # Q4: end_supervision keeps the discharge-half-only clear (no slots 2/3).
    assert 43153 not in writes and 43163 not in writes
    state = writer.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"


# --- fallback entry and the retry while fallback is active (Q1, Q2, Q4) ------

def fallback_site(c: SparkCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "fallback": FALLBACK})
    full_soc(c, monkeypatch)
    ready(c)
    c.connected = False


async def test_a_fallback_entry_cancels_before_the_fallback_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    native_transport.set_holding(43153, [1] * 8)
    fallback_site(writer, monkeypatch)
    native_transport.traffic.clear()
    await writer.fallback.async_evaluate(NOW)
    state = writer.authority.state
    assert state is not None and state.fallback.active
    assert not state.export_supervisions
    writes = [(t.address, t.payload) for t in native_transport.traffic if t.op == "write"]
    clear = (43143, (0,) * 8)
    window = (43143, (23, 30, 5, 30, 0, 0, 0, 0))
    assert clear in writes and window in writes
    assert writes.index(clear) < writes.index(window)
    assert (43153, (0,) * 8) in writes
    assert state.export_records[-1].state == "cleared"


async def test_a_failed_fallback_entry_cancel_is_retried_inside_fallback(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    fallback_site(writer, monkeypatch)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await writer.fallback.async_evaluate(NOW)
    state = writer.authority.state
    assert state is not None and state.fallback.active
    # Q1: the fallback write still proceeds; the record stays un-cleared.
    assert state.export_records[-1].state != "cleared"
    assert ("aborted", "unverified") in transition_kinds(writer)
    native_transport.advance(1)
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.traffic.clear()
    # Q2: the retry runs in fallback and uses the transition policy.
    await writer.async_retry_export_records(NOW)
    state = writer.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"
    assert state.export_records[-1].cleanup_verified is True
    # The verified fallback window's charge half was never disturbed.
    assert slot_registers(native_transport, 43143) == [23, 30, 5, 30, 0, 0, 0, 0]
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_fallback_entry_ends_supervision_so_a_re_offer_is_not_supervised(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    fallback_site(writer, monkeypatch)
    await writer.fallback.async_evaluate(NOW)
    state = writer.authority.state
    assert state is not None and state.fallback.active
    assert not state.export_supervisions
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], epoch=state.epoch))
    # In fallback nothing exports at all, and no supervision survives the entry.
    assert result.intents[0].outcome == "refused"
    assert export_writes(native_transport) == []


# --- restart re-verification (Q5, Q6, Q7) ------------------------------------

async def restart(c: SparkCoordinator) -> SparkCoordinator:
    entry = c.entry
    assert await c.hass.config_entries.async_reload(entry.entry_id)
    await c.hass.async_block_till_done()
    restored: SparkCoordinator = entry.runtime_data
    return restored


async def test_restart_preserves_an_exact_resident_window_without_re_arming(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()],
                                                              sequence=2))
    assert result.outcome in ("applied", "unchanged")
    native_transport.traffic.clear()
    restored = await restart(writer)
    # Startup runs before the first summary validates the device, so the stage
    # cannot re-verify yet: the record is not live until the next tick does.
    assert "inv1" not in restored.writes.reverified
    await restored.async_retry_export_records(NOW)
    state = restored.authority.state
    assert state is not None
    [record] = state.export_records
    assert (record.state, record.cleanup_verified) == ("preserved", False)
    # A window is never re-armed from the record alone: a read, no export write.
    assert export_writes(native_transport) == []
    # The re-verified record is live again, so an untrusted read keeps the apply.
    result = await restored._async_dispatch_command(apply_command(restored, [
        {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": 5.0},
        {"kind": "export", "inverter_id": "inv1", "request": "untrusted"},
    ], sequence=9))
    assert [row.outcome for row in result.intents] == ["unchanged", "unchanged"]
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_restart_clears_a_record_whose_resident_window_mismatches(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [0, 0, 0, 0, 10, 0, 11, 0])
    native_transport.traffic.clear()
    restored = await restart(writer)
    assert restored.authority.state is not None
    assert restored.authority.state.export_records[0].state != "preserved"
    # The clear waits for a validated device (the first accepted summary).
    await restored.async_retry_export_records(NOW)
    state = restored.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"
    assert block_writes(native_transport)[-1][4:] == (0, 0, 0, 0)
    assert not [t for t in native_transport.traffic if t.op == "write" and t.address == 43142]


async def test_restart_cancels_a_record_from_a_different_epoch(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    # A new add-on session bumps the epoch; the resident window still matches.
    assert await writer.authority.async_save(state.model_copy(update={
        "epoch": state.epoch + 1}))
    native_transport.traffic.clear()
    restored = await restart(writer)
    await restored.async_retry_export_records(NOW)
    state = restored.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"
    assert not [t for t in native_transport.traffic
                if t.op == "write" and t.address == 43142]


async def test_restart_honours_a_stop_latch_and_never_preserves(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.authority_store import EventIdentity, StopLatch

    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    event = EventIdentity(direction="export", start=NOW + timedelta(hours=2),
                          end=NOW + timedelta(hours=3))
    assert await writer.authority.async_save(state.model_copy(update={"stop_latches": (
        StopLatch(inverter_id="inv1", event=event, window_end=event.end,
                  epoch=state.epoch),)}))
    restored = await restart(writer)
    await restored.async_retry_export_records(NOW)
    state = restored.authority.state
    assert state is not None
    assert state.export_records[-1].state == "cleared"


async def test_observe_restart_writes_nothing_and_keeps_the_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={
        "committed_mode": "observe", "export_supervisions": ()}))
    native_transport.traffic.clear()
    restored = await restart(writer)
    state = restored.authority.state
    assert state is not None
    assert state.export_records[-1].state != "cleared"
    assert not [t for t in native_transport.traffic
                if t.op == "write" and t.address == 43143]


# --- entry disable cancels before the entry stops (Q4) ----------------------

async def test_disabling_the_entry_cancels_the_export_before_it_stops(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.config_entries import ConfigEntryDisabler

    await armed_window(writer, native_transport, monkeypatch)
    full_soc(writer, monkeypatch)
    ready(writer)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.set_holding(43153, [1] * 8)
    native_transport.traffic.clear()
    assert await writer.hass.config_entries.async_set_disabled_by(
        writer.entry.entry_id, ConfigEntryDisabler.USER)
    assert slot_registers(native_transport, 43143) == [23, 30, 5, 30, 0, 0, 0, 0]
    written_index = next(i for i, t in enumerate(native_transport.traffic)
                         if t.op == "write" and t.address == 43143)
    assert len([t for t in native_transport.traffic[written_index:]
                if t.op == "read" and t.address == 43143]) >= 6
    assert slot_registers(native_transport, 43153) == [0] * 8
    store = writer.control.store
    assert store.state is not None
    assert store.state.committed_mode == "observe"
    assert store.state.export_records[-1].state == "cleared"


# --- removal after unload (Q13) ---------------------------------------------

async def test_removal_after_unload_keeps_the_record_and_logs_the_abort(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from homeassistant.components import persistent_notification

    await armed_window(writer, native_transport, monkeypatch)
    full_soc(writer, monkeypatch)
    ready(writer)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    native_transport.traffic.clear()
    await writer.hass.config_entries.async_remove(writer.entry.entry_id)
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert any(call.kwargs.get("title") == "ha-spark handover incomplete"
               for call in notice.call_args_list)
    # The store loads again for the handover attempt: the obligation stays visible.
    from custom_components.ha_spark.authority import AuthorityStore

    store = AuthorityStore(writer.hass, writer.entry.entry_id, lambda: None)
    await store.async_load()
    assert store.state is not None
    assert store.state.export_records[-1].state != "cleared"
    assert [row.reason for row in store.state.export_transitions
            if row.kind == "aborted"] == ["read_only"]


# --- activation and fallback exit judge charge intents only (Q15) -----------

async def test_a_deferred_export_does_not_block_plan_verified(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    far = {"direction": "export", "start": (NOW + timedelta(hours=26)).isoformat(),
           "end": (NOW + timedelta(hours=27)).isoformat()}
    await arm(writer, event=far)
    state = writer.authority.state
    assert state is not None
    result = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), window_intent(event=far)]))
    assert result.outcome == "deferred"
    assert writer.writes.plan_verified(state.epoch, since=NOW - timedelta(seconds=1))


async def test_a_refused_export_with_a_safe_resident_state_does_not_block_plan_verified(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await arm(writer)
    native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
    writer.native.readings["inv1"]["input_33022"] = Reading(NOW, (26, 11, 2, 11, 0, 0), 0)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    state = writer.authority.state
    assert state is not None
    result = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), window_intent()]))
    assert result.outcome == "refused"
    assert block_writes(native_transport)[-1][4:] == (0, 0, 0, 0)
    assert writer.writes.plan_verified(state.epoch, since=NOW - timedelta(seconds=1))


async def test_a_consumed_command_is_never_replayed_after_fallback_exit(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.ha_spark.command_dispatch import CommandConsumer

    allow_export(writer, monkeypatch, native_transport)
    far = {"direction": "export", "start": (NOW + timedelta(hours=26)).isoformat(),
           "end": (NOW + timedelta(hours=27)).isoformat()}
    await arm(writer, event=far)
    state = writer.authority.state
    assert state is not None
    consumer = CommandConsumer(
        "test", lambda: writer.authority.state.epoch if writer.authority.state else None,
        writer._async_dispatch_command)
    deferred = apply_command(writer, [window_intent(event=far)], sequence=7)
    assert (await consumer.async_consume(deferred)).outcome == "deferred"
    native_transport.traffic.clear()
    replay = await consumer.async_consume(deferred.model_copy())
    assert (replay.outcome, replay.reason) == ("deferred", None)
    assert not [t for t in native_transport.traffic if t.op == "write"]
    fresh = apply_command(writer, [window_intent(event=far)], sequence=8)
    assert (await consumer.async_consume(fresh)).outcome == "deferred"


# --- W9-2: an identity or claim change ends the bound ------------------------

async def test_a_readback_mismatch_reprobe_that_changes_identity_ends_the_bound(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_input(35000, [0x2032])
    await writer.native.async_readback_mismatch("inv1")
    assert "inv1" in writer.native.identity_changed
    # Reverting the identity before the tick must not revive the ended binding.
    native_transport.set_input(35000, [0x2031])
    await writer.native.async_readback_mismatch("inv1")
    state = writer.authority.state
    assert state is not None and state.export_supervisions
    await writer.async_drop_stale_bounds(now=NOW)
    state = writer.authority.state
    assert state is not None and not state.export_supervisions
    assert not writer.native.identity_changed


async def test_an_unchanged_identity_reprobe_keeps_the_bound(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    await writer.native.async_readback_mismatch("inv1")
    await writer.async_drop_stale_bounds(now=NOW)
    state = writer.authority.state
    assert state is not None and state.export_supervisions


async def test_a_claim_edit_ends_the_bound_but_the_same_claim_keeps_it(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    await writer.async_drop_stale_bounds(
        claims=[{"host": "gateway.invalid", "port": 502, "unit_id": 1}], now=NOW)
    state = writer.authority.state
    assert state is not None and state.export_supervisions
    # The options flow saves the new claims and asks for the same drop.
    await writer.async_drop_stale_bounds(
        claims=[{"host": "gateway.invalid", "port": 502, "unit_id": 2}], now=NOW)
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_a_claim_edited_while_unloaded_ends_the_bound_on_setup(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    writer.hass.config_entries.async_update_entry(writer.entry, options={
        **writer.entry.options,
        "gateway_claims": [{"host": "gateway.invalid", "port": 502, "unit_id": 2}],
    })
    restored = await restart(writer)
    state = restored.authority.state
    assert state is not None and not state.export_supervisions


async def test_a_stale_record_is_cleaned_up_in_protect_too(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    # A transition out of Optimise that could not finish its cancel leaves the
    # record behind in committed Protect.
    assert await writer.authority.async_save(state.model_copy(update={
        "committed_mode": "protect", "export_supervisions": (),
        "epoch": state.epoch + 1}))
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    await writer.async_retry_export_records(NOW)
    state = writer.authority.state
    assert state is not None
    assert state.committed_mode == "protect"
    assert state.export_records[-1].state == "cleared"
    assert slot_registers(native_transport, 43143)[4:] == [0, 0, 0, 0]
    assert not export_writes(native_transport)


async def test_an_options_flow_claim_edit_drops_the_bound_immediately(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    from homeassistant.data_entry_flow import FlowResultType

    from tests_ha.test_topology_flow import choose, open_topology

    await armed_window(writer, native_transport, monkeypatch)
    flow_id = await open_topology(writer.hass, writer.entry)
    await choose(writer.hass, flow_id, "inverters")
    await choose(writer.hass, flow_id, "inverters_edit")
    await writer.hass.config_entries.options.async_configure(flow_id, {"id": "inv1"})
    item = dict(writer.entry.options["topology"]["inverters"][0])
    item["gateway_unit_id"] = 2
    result = await writer.hass.config_entries.options.async_configure(flow_id, item)
    assert result["step_id"] == "native_connect"
    await writer.hass.config_entries.options.async_configure(flow_id, {})
    await choose(writer.hass, flow_id, "inverters_done")
    with patch.object(writer.hass.config_entries, "async_reload", return_value=True):
        result = await choose(writer.hass, flow_id, "save")
        await writer.hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert writer.entry.options["gateway_claims"] == [
        {"host": "gateway.invalid", "port": 502, "unit_id": 2}]
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_an_identity_change_across_a_failed_probe_still_ends_the_bound(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.connected = False
    await writer.native.async_readback_mismatch("inv1")
    native_transport.connected = True
    native_transport.set_input(35000, [0x2032])
    await writer.native.async_readback_mismatch("inv1")
    assert "inv1" in writer.native.identity_changed


async def test_a_bound_whose_identity_differs_from_the_probe_is_dropped(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # After a restart there is no latch: the stored identity itself is compared.
    await armed_window(writer, native_transport, monkeypatch)
    writer.native.identity_changed.clear()
    writer.native.identities["inv1"] = (0x0026, 0x2032, 0x0000)
    await writer.async_drop_stale_bounds(now=NOW)
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_an_admin_reprobe_back_to_the_old_identity_keeps_the_latch(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_input(35000, [0x2032])
    await writer.native.async_readback_mismatch("inv1")
    native_transport.set_input(35000, [0x2031])
    await writer.async_reprobe("inv1")
    await writer.async_drop_stale_bounds(now=NOW)
    state = writer.authority.state
    assert state is not None and not state.export_supervisions


async def test_a_restart_in_fallback_keeps_the_slot_two_three_cancel_obligation(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [0, 0, 0, 0, 14, 0, 15, 0])
    native_transport.set_holding(43153, [1] * 8)
    fallback_site(writer, monkeypatch)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43153)
    await writer.fallback.async_evaluate(NOW)
    state = writer.authority.state
    assert state is not None and state.fallback.active
    assert state.export_records[-1].state != "cleared"
    restored = await restart(writer)
    full_soc(restored, monkeypatch)
    ready(restored)
    native_transport.set_holding(43153, [1] * 8)
    await restored.async_retry_export_records(NOW)
    state = restored.authority.state
    assert state is not None
    # Retired only by the fallback-entry cancel, which zeroes slots 2/3 too.
    assert state.export_records[-1].state == "cleared"
    assert slot_registers(native_transport, 43153) == [0] * 8


async def test_a_restart_never_preserves_from_an_unvalidated_read(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed_window(writer, native_transport, monkeypatch)
    result = await writer._async_dispatch_command(apply_command(writer, [window_intent()],
                                                              sequence=2))
    assert result.outcome in ("applied", "unchanged")
    native_transport.caching = True
    restored = await restart(writer)
    assert restored.native.reason(next(iter(restored.native.inverters.values()))) is not None
    state = restored.authority.state
    assert state is not None
    assert state.export_records[-1].state != "preserved"
    assert "inv1" not in restored.writes.reverified
