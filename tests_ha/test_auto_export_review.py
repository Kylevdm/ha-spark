"""Regression cases for automatic export supervision review findings."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest

from custom_components.ha_spark import (
    flexibility_events,
    native_export_log,
    native_export_supervision,
)
from custom_components.ha_spark.authority_store import AuthorityState
from custom_components.ha_spark.commands import CommandResult, IntentResult
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_export import event_from_raw
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_auto_export import admin as admin_fixture
from tests_ha.test_auto_export import clock, enable  # noqa: F401
from tests_ha.test_auto_export import writer as writer_fixture
from tests_ha.test_flexibility_events import submit, switch
from tests_ha.test_native_export import EVENT, NOW, apply_command, command, window_intent
from tests_ha.test_native_export_interplay import restart
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401

writer = writer_fixture
admin = admin_fixture


@pytest.mark.parametrize("failed_switch", [False, True])
async def test_reprobe_cannot_drop_consent_evidence_before_switch_off(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
    failed_switch: bool,
) -> None:
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    rows = writer.authority.state.export_supervisions
    assert rows
    native_transport.set_input(35000, [0x2032])
    if failed_switch:
        with patch.object(writer.hass.config_entries, "async_update_entry"):
            await writer.async_reprobe("inv1")
        assert writer.authority.state.export_supervisions == rows
        assert "inv1" in writer.native.identity_changed
    else:
        await writer.async_reprobe("inv1")
        assert not writer.authority.state.export_supervisions
        assert not flexibility_events.stored(writer.entry.options.get(
            flexibility_events.OPTION_KEY)).export_during_events


async def test_first_probe_after_restart_revokes_old_rating(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
) -> None:
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None and writer.authority.state.export_supervisions
    native_transport.set_input(35000, [0x2032])
    restored = await restart(writer)
    await restored.async_drop_stale_bounds()
    assert not flexibility_events.stored(restored.entry.options.get(
        flexibility_events.OPTION_KEY)).export_during_events
    assert restored.authority.state is not None
    assert not restored.authority.state.export_supervisions


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_endpoint_swap_is_a_rated_claim_change(
    writer: SparkCoordinator, admin: None,
) -> None:
    from homeassistant.data_entry_flow import FlowResultType

    from tests_ha.test_topology_flow import choose, open_topology

    await submit(writer, {"rating_inv1": 1000, "rating_inv2": 1000})
    await switch(writer, True)
    items = writer.entry.options["topology"]["inverters"]
    flow_id = await open_topology(writer.hass, writer.entry)
    await choose(writer.hass, flow_id, "inverters")
    for index in range(2):
        await choose(writer.hass, flow_id, "inverters_edit")
        await writer.hass.config_entries.options.async_configure(
            flow_id, {"id": items[index]["id"]})
        item = dict(items[index])
        peer = items[1 - index]
        item["gateway_host"] = peer["gateway_host"]
        item["gateway_port"] = peer.get("gateway_port", 502)
        item["gateway_unit_id"] = peer.get("gateway_unit_id", 1)
        result = await writer.hass.config_entries.options.async_configure(flow_id, item)
        assert result["step_id"] == "native_connect"
        await writer.hass.config_entries.options.async_configure(flow_id, {})
    await choose(writer.hass, flow_id, "inverters_done")
    with patch.object(writer.hass.config_entries, "async_reload", return_value=True):
        result = await choose(writer.hass, flow_id, "save")
        await writer.hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not flexibility_events.stored(writer.entry.options.get(
        flexibility_events.OPTION_KEY)).export_during_events


async def test_unverified_clear_without_record_blocks_replacement_this_apply(
    writer: SparkCoordinator, admin: None,
) -> None:
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    assert writer.authority.state is not None
    assert writer.authority.state.export_supervisions
    assert await writer.authority.async_save(writer.authority.state.model_copy(update={
        "export_records": (),
    }))
    moved = {**EVENT, "end": (NOW + timedelta(hours=4)).isoformat()}
    with patch.object(writer.writes, "async_clear_export", return_value=IntentResult(
        outcome="unverified")):
        await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    assert not writer.authority.state.export_supervisions
    await writer._async_dispatch_command(apply_command(writer, [window_intent(event=moved)]))
    assert writer.authority.state.export_supervisions[0].event.end == NOW + timedelta(hours=4)


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_trip_evidence_retires_remaining_automatic_rows(
    writer: SparkCoordinator, admin: None,
) -> None:
    topology = deepcopy(writer.entry.options["topology"])
    topology["connections"].append({**topology["connections"][0], "id": "grid2"})
    topology["banks"].append({**topology["banks"][0], "id": "bank2", "connection_id": "grid2"})
    topology["inverters"][1]["bank_id"] = "bank2"
    writer.hass.config_entries.async_update_entry(
        writer.entry, options={**writer.entry.options, "topology": topology})
    writer = await restart(writer)
    await submit(writer, {"rating_inv1": 1000, "rating_inv2": 1000})
    await switch(writer, True)
    intents = [window_intent(), window_intent(inverter_id="inv2")]
    await writer._async_dispatch_command(apply_command(writer, intents))
    state = writer.authority.state
    event = event_from_raw(EVENT)
    assert state is not None and event is not None
    assert len(state.export_supervisions) == 2
    # Connection A has tripped; connection B still has its durable permission.
    state = native_export_log.with_append(state, inverter_id="inv1", event=event,
                                          kind="tripped", at=NOW)
    assert await writer.authority.async_save(state.model_copy(update={
        "export_supervisions": tuple(r for r in state.export_supervisions
                                     if r.inverter_id == "inv2"), "stop_latches": (),
    }))
    await writer._async_dispatch_command(apply_command(writer, intents))
    assert writer.authority.state is not None
    assert not writer.authority.state.export_supervisions
    assert any(r.kind == "refused" and r.reason == "tripped"
               for r in writer.authority.state.export_transitions)


async def test_stale_apply_cannot_reconcile_a_moved_event(
    writer: SparkCoordinator, admin: None, native_transport: FakeSolisDevice,
) -> None:
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    state = writer.authority.state
    assert state is not None and state.export_supervisions
    native_transport.traffic.clear()
    moved = {**EVENT, "end": (NOW + timedelta(hours=4)).isoformat()}
    with patch("custom_components.ha_spark.notify_target.async_notice", new_callable=Mock) as n:
        await writer._async_dispatch_command(apply_command(
            writer, [window_intent(event=moved)], epoch=state.epoch + 1))
    assert writer.authority.state is not None
    assert writer.authority.state.export_supervisions == state.export_supervisions
    assert [r for r in writer.authority.state.export_transitions if r.source == "automatic"] == [
        r for r in state.export_transitions if r.source == "automatic"]
    assert not [t for t in native_transport.traffic if t.op == "write"]
    n.assert_not_called()


async def test_manual_stale_authority_precedes_invalid_event(
    writer: SparkCoordinator,
) -> None:
    result = await writer._async_dispatch_command(command(
        writer, epoch=999, event={**EVENT, "direction": "import"}))
    assert result.reason == "stale_authority"


async def test_refusal_cannot_evict_live_held_abort_evidence(writer: SparkCoordinator) -> None:
    state = writer.authority.state
    assert state is not None
    for index in range(native_export_log.MAX_PER_INVERTER // 2):
        event = event_from_raw({
            **EVENT, "end": (NOW + timedelta(hours=3, minutes=index)).isoformat()})
        assert event is not None
        state = native_export_log.with_append(state, inverter_id="inv1", event=event,
                                              kind="unverified", at=NOW)
        state = native_export_log.with_append(state, inverter_id="inv1", event=event,
                                              kind="aborted", at=NOW)
    event = event_from_raw(EVENT)
    assert event is not None
    candidate = native_export_log.with_append(state, inverter_id="inv1", event=event,
        kind="refused", reason="mode", source="automatic", at=NOW)
    assert candidate == state


@pytest.mark.parametrize("await_boundary", ["admin", "core", "save"])
async def test_event_ending_during_await_has_no_refusal_notice(
    writer: SparkCoordinator, admin: None,
    monkeypatch: pytest.MonkeyPatch, await_boundary: str,
) -> None:
    await enable(writer)
    now = Mock(return_value=NOW)
    monkeypatch.setattr(native_export_supervision, "dt_util", SimpleNamespace(utcnow=now))
    state = writer.authority.state
    event = event_from_raw(EVENT)
    assert state is not None and event is not None
    assert await writer.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    if await_boundary == "admin":
        async def get_user(user_id: str) -> SimpleNamespace:
            now.return_value = event.end
            return SimpleNamespace(is_admin=False, system_generated=False)
        monkeypatch.setattr(writer.hass.auth, "async_get_user", get_user)
    elif await_boundary == "core":
        supervise = writer._async_supervise_event_locked

        async def delayed_supervise(*args: Any, **kwargs: Any) -> CommandResult:
            result = await supervise(*args, **kwargs)
            now.return_value = event.end
            return result
        monkeypatch.setattr(writer, "_async_supervise_event_locked", delayed_supervise)
    else:
        save = writer.authority.async_save

        async def delayed_save(candidate: AuthorityState) -> bool:
            result = await save(candidate)
            now.return_value = event.end
            return result
        monkeypatch.setattr(writer.authority, "async_save", delayed_save)
    with patch("custom_components.ha_spark.notify_target.async_notice", new_callable=Mock) as n:
        await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    n.assert_not_called()
    if await_boundary in ("admin", "core"):
        assert writer.authority.state is not None
        assert not [r for r in writer.authority.state.export_transitions if r.kind == "refused"]


async def test_manual_then_automatic_acceptance_records_both_sources(
    writer: SparkCoordinator, admin: None,
) -> None:
    await writer._async_dispatch_command(command(writer))
    assert writer.authority.state is not None
    [row] = writer.authority.state.export_supervisions
    await writer._async_dispatch_command(command(writer, kind="end_supervision",
                                                 supervision_id=str(row.id)))
    await enable(writer)
    await writer._async_dispatch_command(apply_command(writer, [window_intent()]))
    accepted = [r for r in writer.authority.state.export_transitions if r.kind == "accepted"]
    assert [r.source for r in accepted] == ["manual", "automatic"]
