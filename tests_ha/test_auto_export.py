"""Export during events at the integration apply boundary."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.ha_spark import native_export_log
from custom_components.ha_spark.authority_store import StopLatch
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.flexibility_events import OPTION_KEY, stored
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_flexibility_events import admin as admin_fixture
from tests_ha.test_flexibility_events import submit, switch
from tests_ha.test_native_export import (
    EVENT,
    NOW,
    allow_export,
    apply_command,
    block_writes,
    command,
    window_intent,
)
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer

writer = current_writer
admin = admin_fixture


@pytest.fixture(autouse=True)
def clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


async def enable(c: SparkCoordinator) -> None:
    result = await submit(c, {"rating_inv1": 3000})
    assert result["type"].value == "create_entry"
    await switch(c, True)


async def test_apply_supervises_and_arms_once(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: None,
) -> None:
    await enable(writer)
    allow_export(writer, monkeypatch, native_transport)
    with patch("custom_components.ha_spark.notify_target.async_notice") as notice:
        result = await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert result.intents[0].outcome == "applied"
        assert result.intents[0].written_a == 62.5
        state = writer.authority.state
        assert state is not None
        [row] = state.export_supervisions
        assert row.provenance == "automatic" and row.confirming_user_id == "user-id"
        assert row.rating_w == 3000
        assert block_writes(native_transport) == [(0, 0, 0, 0, 14, 0, 15, 0)]
        assert any(t.op == "write" and t.address == 43142 and t.payload == (625,)
                   for t in native_transport.traffic)
        [accepted] = [t for t in state.export_transitions if t.kind == "accepted"]
        assert accepted.source == "automatic"
        assert writer.guard is not None
        [snapshot] = writer._export_snapshot(state, writer.guard.topology)
        transitions = snapshot["transitions"]
        assert isinstance(transitions, list)
        assert all("source" not in transition and transition["kind"] != "refused"
                   for transition in transitions)
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state is not None
        assert writer.authority.state.export_supervisions == (row,)
        assert len([t for t in writer.authority.state.export_transitions
                    if t.kind == "accepted"]) == 1
        notice.assert_not_called()
        await switch(writer, False)
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state.export_supervisions == (row,)


@pytest.mark.parametrize("mode", ["observe", "protect", "manual", "fallback"])
async def test_mode_refuses_once_then_retries(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: None, mode: str,
) -> None:
    await enable(writer)
    allow_export(writer, monkeypatch, native_transport)
    state = writer.authority.state
    assert state is not None
    from custom_components.ha_spark.authority_store import FallbackState, ManualSession

    updates: dict[str, object] = {"committed_mode": mode} if mode in (
        "observe", "protect") else {}
    if mode == "fallback":
        updates["fallback"] = FallbackState(active=True, causes=("soc_integrity",), entered_at=NOW)
    if mode == "manual":
        updates["manual_session"] = ManualSession(
            return_to="optimise", keep_protection=True)
    assert await writer.authority.async_save(state.model_copy(update=updates))
    with patch("custom_components.ha_spark.notify_target.async_notice",
               new_callable=Mock) as n:
        for _ in range(2):
            await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state is not None
        assert not writer.authority.state.export_supervisions
        assert not block_writes(native_transport)
        assert n.call_count == 1
        refused = [t for t in writer.authority.state.export_transitions if t.kind == "refused"]
        assert len(refused) == 1 and refused[0].reason == "mode"
        assert not native_export_log.visible(refused, "inv1")
        assert await writer.authority.async_save(state)
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state.export_supervisions


async def test_off_and_ended_never_supervise(
    writer: SparkCoordinator, admin: None, freezer: FrozenDateTimeFactory,
    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    with patch("custom_components.ha_spark.notify_target.async_notice") as n:
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert not [t for t in native_transport.traffic if t.op == "write"]
        assert writer.authority.state is not None
        assert not writer.authority.state.export_supervisions
        await enable(writer)
        freezer.move_to(NOW + timedelta(hours=3))
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert not writer.authority.state.export_supervisions
        n.assert_not_called()


async def test_untrusted_preserves_and_trusted_cancel_clears(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: None,
) -> None:
    await enable(writer)
    allow_export(writer, monkeypatch, native_transport)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    rows = writer.authority.state.export_supervisions
    native_transport.traffic.clear()
    await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "export", "inverter_id": "inv1", "request": "untrusted"}]))
    assert writer.authority.state.export_supervisions == rows
    assert not native_transport.traffic
    await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "export", "inverter_id": "inv1", "request": "none"}]))
    assert not writer.authority.state.export_supervisions
    [record] = writer.authority.state.export_records
    assert record.state == "cleared" and record.cleanup_verified
    assert all(native_transport.holding[address] == 0 for address in range(43147, 43151))
    assert len([t for t in native_transport.traffic
                if t.op == "read" and t.address == 43143]) >= 6


async def test_manual_row_is_never_replaced(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    await writer._async_dispatch_command(command(writer))
    assert writer.authority.state is not None
    rows = writer.authority.state.export_supervisions
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state.export_records[-1].supervision_id == rows[0].id
    assert writer.authority.state.export_records[-1].state == "armed"
    moved = {**EVENT, "end": (NOW + timedelta(hours=4)).isoformat()}
    await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    assert writer.authority.state.export_supervisions == rows


async def test_trip_evidence_outlives_window_latch(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
) -> None:
    await enable(writer)
    from custom_components.ha_spark.native_export import event_from_raw
    from tests_ha.test_native_export_interplay import restart

    event = event_from_raw(EVENT)
    assert event is not None
    state = writer.authority.state
    assert state is not None
    state = native_export_log.with_append(state, inverter_id="inv1", event=event,
                                          kind="tripped", at=NOW)
    assert await writer.authority.async_save(state.model_copy(update={"stop_latches": (
        StopLatch(inverter_id="inv1", event=event, window_end=NOW, epoch=state.epoch),)}))
    await writer.async_expire_supervisions(NOW)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert any(t.kind == "refused" and t.reason == "tripped"
               for t in writer.authority.state.export_transitions)
    restored = await restart(writer)
    native_transport.traffic.clear()
    await restored._async_dispatch_command(apply_command(restored, [window_intent()]))
    assert restored.authority.state is not None
    assert not restored.authority.state.export_supervisions
    assert not block_writes(native_transport)


async def test_apply_waits_for_control_lock(writer: SparkCoordinator, admin: None) -> None:
    await enable(writer)
    async with writer.control.lock:
        task = asyncio.create_task(writer._async_dispatch_command(
            apply_command(writer, [window_intent()])))
        await asyncio.sleep(0)
        assert not task.done()
    await asyncio.wait_for(task, 5)
    assert writer.authority.state is not None
    assert writer.authority.state.export_supervisions


@pytest.mark.parametrize("status", ["missing", "not_admin", "system"])
async def test_admin_is_reverified(writer: SparkCoordinator, admin: None,
                                   monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    await enable(writer)
    user = (None if status == "missing" else SimpleNamespace(
        is_admin=status != "not_admin", system_generated=status == "system"))
    monkeypatch.setattr(writer.hass.auth, "async_get_user", AsyncMock(return_value=user))
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert any(t.reason == "not_admin" for t in writer.authority.state.export_transitions)


@pytest.mark.parametrize("existing", [False, True])
async def test_identity_change_switches_off_without_needing_a_row(
    writer: SparkCoordinator, admin: None, existing: bool,
    native_transport: FakeSolisDevice,
) -> None:
    await enable(writer)
    if existing:
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    native_transport.set_input(35000, [0x2032])
    await writer.native.async_readback_mismatch("inv1")
    await writer.async_drop_stale_bounds()
    assert not stored(writer.entry.options[OPTION_KEY]).export_during_events


async def test_moved_event_clears_before_replacement(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: None,
) -> None:
    await enable(writer)
    allow_export(writer, monkeypatch, native_transport)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    moved = {**EVENT, "end": (NOW + timedelta(hours=4)).isoformat()}
    # A write that cannot verify must not grant the replacement event permission.
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43143)
    await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert all(r.event.end != NOW + timedelta(hours=4)
               for r in writer.authority.state.export_records)
    assert writer.authority.state.export_records[-1].state == "cleared"
    assert writer.authority.state.export_records[-1].cleanup_verified
    await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    assert writer.authority.state.export_supervisions[0].event.end == NOW + timedelta(hours=4)


async def test_changed_rating_is_checked_again(
    writer: SparkCoordinator, admin: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await enable(writer)
    monkeypatch.setattr(writer.native, "probes", {})
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert any(t.kind == "refused" and t.reason == "export_bound_unverified"
               for t in writer.authority.state.export_transitions)


async def test_failed_save_has_no_effects(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, admin: None,
) -> None:
    await enable(writer)
    allow_export(writer, monkeypatch, native_transport)
    before = writer.authority.state
    with patch.object(writer.authority, "async_save", return_value=False), patch(
        "custom_components.ha_spark.notify_target.async_notice", new_callable=Mock,
    ) as n:
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state == before
        assert not block_writes(native_transport)
        n.assert_not_called()


async def test_restart_retries_a_refused_event(
    writer: SparkCoordinator, admin: None, monkeypatch: pytest.MonkeyPatch,
    native_transport: FakeSolisDevice,
) -> None:
    from tests_ha.test_native_export_interplay import restart

    await enable(writer)
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    with patch("custom_components.ha_spark.notify_target.async_notice",
               new_callable=Mock) as n:
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert n.call_count == 1
        restored = await restart(writer)
        await restored._async_dispatch_command(apply_command(restored, [window_intent()]))
        assert n.call_count == 1
        state = restored.authority.state
        assert state is not None
        assert await restored.authority.async_save(state.model_copy(
            update={"committed_mode": "optimise"}))
        allow_export(restored, monkeypatch, native_transport)
        await restored._async_dispatch_command(apply_command(restored, [window_intent()]))
        assert restored.authority.state is not None
        assert restored.authority.state.export_supervisions


@pytest.mark.parametrize("export_request", ["untrusted", "none", "malformed", "different"])
@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_nonunanimous_apply_never_supervises(
    writer: SparkCoordinator, admin: None, export_request: str,
) -> None:
    await submit(writer, {"rating_inv1": 1000, "rating_inv2": 1000})
    await switch(writer, True)
    other = ({"kind": "export", "inverter_id": "inv2", "request": export_request}
             if export_request != "different" else window_intent(
                 inverter_id="inv2",
                 event={**EVENT, "end": (NOW + timedelta(hours=4)).isoformat()}))
    await writer._async_dispatch_command(apply_command(writer, [window_intent(), other]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions


async def test_no_identity_change_keeps_opt_in(writer: SparkCoordinator, admin: None) -> None:
    await enable(writer)
    options = dict(writer.entry.options)
    await writer.async_drop_stale_bounds(claims=writer.entry.options.get("gateway_claims"))
    assert dict(writer.entry.options) == options


async def test_claim_edit_flow_writes_switched_off_options(
    writer: SparkCoordinator, admin: None,
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    from tests_ha.test_topology_flow import choose, open_topology

    await enable(writer)
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
    with patch.object(writer.hass.config_entries, "async_reload", return_value=True), patch(
        "custom_components.ha_spark.notify_target.async_notice", new_callable=Mock,
    ) as n:
        result = await choose(writer.hass, flow_id, "save")
        await writer.hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert not stored(writer.entry.options[OPTION_KEY]).export_during_events
        assert n.call_count == 1


async def test_exception_in_automatic_step_does_not_change_charge_result(
    writer: SparkCoordinator,
) -> None:
    from tests_ha.test_native_export import charge_intents

    with patch.object(writer, "async_supervise_export_intents", side_effect=RuntimeError):
        result = await writer._async_dispatch_command(apply_command(writer, [charge_intents()[1]]))
    assert all(r.outcome in ("applied", "unchanged") for r in result.intents)


async def test_stale_epoch_cannot_supervise(writer: SparkCoordinator, admin: None) -> None:
    await enable(writer)
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={"epoch": state.epoch + 1}))
    await writer._async_dispatch_command(apply_command(
        writer, [window_intent()], epoch=state.epoch))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert any(t.kind == "refused" and t.reason == "stale_authority"
               for t in writer.authority.state.export_transitions)


async def test_failed_refusal_save_is_retried_without_a_notice(
    writer: SparkCoordinator, admin: None,
) -> None:
    await enable(writer)
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    with patch("custom_components.ha_spark.notify_target.async_notice",
               new_callable=Mock) as n:
        with patch.object(writer.authority, "async_save", return_value=False):
            await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert writer.authority.state is not None
        assert not writer.authority.state.export_transitions
        n.assert_not_called()
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
        assert n.call_count == 1


async def test_refusal_rows_prune_without_affecting_lifecycle_evidence(
    writer: SparkCoordinator,
) -> None:
    from custom_components.ha_spark.native_export import event_from_raw

    event = event_from_raw(EVENT)
    state = writer.authority.state
    assert state is not None and event is not None
    for reason in ("mode", "not_admin", "mode"):
        state = native_export_log.with_append(state, inverter_id="inv1", event=event,
            kind="refused", reason=reason, source="automatic", at=NOW)
    assert len(state.export_transitions) == 2
    assert not native_export_log.visible(state.export_transitions, "inv1")
    assert not native_export_log.held_at_start(state.export_transitions, event.start)
    assert not native_export_log.held_aborted(state.export_transitions, "inv1", event)
    kept, _ = native_export_log.trim(state.export_transitions, NOW, reserve=64,
                                     reserve_inverter="inv1")
    assert not kept
    assert not native_export_log.with_pruned(state, event.end).export_transitions


async def test_failed_identity_switch_off_still_blocks_acceptance(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
) -> None:
    await enable(writer)
    native_transport.set_input(35000, [0x2032])
    await writer.native.async_readback_mismatch("inv1")
    with patch.object(writer.hass.config_entries, "async_update_entry"):
        assert not await writer.async_drop_stale_bounds()
    assert stored(writer.entry.options[OPTION_KEY]).export_during_events
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
