"""Native durable holds through the real config entry and the in-memory inverter."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import native_power
from custom_components.ha_spark.authority_store import HoldRecord, ManualSession, Transition
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_hold import hold_device
from custom_components.ha_spark.transitions import TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
async def held_site(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                    native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
                    freezer: Any) -> SparkCoordinator:
    freezer.move_to("2026-11-02T12:00:00Z")
    async def sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(native_power, "verify_sleep", sleep)
    native_transport.set_read_frames()
    native_transport.set_holding(43007, [190])
    entry = await native_entry(hass, aioclient_mock)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    c: SparkCoordinator = entry.runtime_data
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    native_transport.traffic.clear()
    return c


async def tick(c: SparkCoordinator) -> None:
    await c.holds.async_reconcile(datetime.now(UTC))


async def test_hold_start_persists_before_off_and_confirms_only_after_readback(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    original = native_transport.write
    async def write(address: int, values: list[int], *, device_id: int = 1) -> Any:
        state = c.authority.state
        assert state is not None and state.holds and not state.holds[0].confirmed
        return await original(address, values, device_id=device_id)
    monkeypatch.setattr(native_transport, "write", write)
    await tick(c)
    assert native_transport.holding[43007] == 222
    state = c.authority.state
    assert state is not None and state.holds[0].confirmed


async def test_owner_off_is_never_adopted_or_released(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    native_transport.set_holding(43007, [222])
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    assert c.authority.state is not None and not c.authority.state.holds
    c.hass.states.async_set("sensor.ev", "stopped")
    assert await c.holds.async_release(datetime.now(UTC))
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_five_fresh_reports_release_without_changing_manual_schedule(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, freezer: Any,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    native_transport.set_holding(43141, [321, 0, 23, 30, 5, 30] + [0] * 24)
    native_transport.traffic.clear()
    for n in range(5):
        freezer.tick(timedelta(seconds=1))
        c.hass.states.async_set("sensor.ev", "stopped")
        await tick(c)
        if n < 4:
            assert native_transport.holding[43007] == 222
            await tick(c)  # repeated cached report never counts twice
            assert native_transport.holding[43007] == 222
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds
    assert all(t.address == 43007 for t in native_transport.traffic if t.op == "write")
    # Restoration reads precede On while still Off; no schedule is replayed.
    assert any(t.address == 43141 and t.op == "read" for t in native_transport.traffic)


async def test_unknown_authority_or_observe_writes_nothing(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    await tick(c)
    monkeypatch.setattr(c.authority, "status", "unknown")
    await tick(c)
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_unconfirmed_off_request_does_not_become_owned_on_restart(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    state = c.authority.state
    assert state is not None
    inv = next(iter(c.native.inverters.values()))
    await c.authority.async_save(state.model_copy(update={"holds": (HoldRecord(
        cause="ev", device=hold_device(inv), started_at=datetime.now(UTC)),)}))
    native_transport.set_holding(43007, [222])
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    assert c.authority.state is not None and not c.authority.state.holds[0].confirmed
    await c.holds.async_release(datetime.now(UTC))
    assert native_transport.holding[43007] == 222
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_unverified_hold_is_retained_and_notified(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    assert c.authority.state is not None and not c.authority.state.holds[0].confirmed
    assert c.holds.notices


async def test_confirmed_hold_survives_real_reload_and_stale_ev(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    entry = c.entry
    native_transport.traffic.clear()
    assert await c.hass.config_entries.async_reload(entry.entry_id)
    await c.hass.async_block_till_done()
    restored: SparkCoordinator = entry.runtime_data
    restored.hass.states.async_set("sensor.ev", "unavailable")
    await tick(restored)
    assert restored.authority.state is not None and restored.authority.state.holds[0].confirmed
    assert native_transport.holding[43007] == 222
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_dispatch_ends_at_last_trusted_end_when_source_is_stale(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, freezer: Any,
) -> None:
    c = held_site
    now = datetime.now(UTC)
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300},
        "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 300}}})
    c.hass.states.async_set("sensor.ev", "stopped")
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [{
        "start": now.isoformat(), "end": (now + timedelta(minutes=10)).isoformat()}]})
    await tick(c)
    assert native_transport.holding[43007] == 222
    freezer.tick(timedelta(minutes=6))
    await tick(c)
    assert native_transport.holding[43007] == 222
    freezer.tick(timedelta(minutes=4))
    await tick(c)
    assert native_transport.holding[43007] == 190
    assert c.authority.state is not None and not c.authority.state.holds


async def test_failed_restore_never_writes_on_and_retains_owned_record(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    native_transport.holding.pop(43141)
    native_transport.traffic.clear()
    assert not await c.holds.async_release(datetime.now(UTC))
    assert c.authority.state is not None and c.authority.state.holds[0].confirmed
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_remap_fences_holds_charge_and_clock(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    topology = c.entry.options["topology"]
    topology = {**topology, "inverters": [{**topology["inverters"][0], "gateway_unit_id": 2}]}
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options,
                                                              "topology": topology})
    native_transport.traffic.clear()
    await tick(c)
    inv = next(iter(c.native.inverters.values()))
    assert c.native.reason(inv) == "held_identity_remapped"
    assert not await c.holds.async_release(datetime.now(UTC))
    assert (await c.clock.async_sync(inv.id)).outcome == "refused"
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_removal_requires_fresh_clear_evidence_not_merely_an_owner_request(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    native_transport.traffic.clear()
    assert not await c.holds.async_release(datetime.now(UTC), removal=True)
    assert not any(t.op == "write" for t in native_transport.traffic)
    c.hass.states.async_set("sensor.ev", "stopped")
    assert await c.holds.async_release(datetime.now(UTC), removal=True)
    assert native_transport.holding[43007] == 190


async def test_startup_and_protect_transition_use_same_hold_evaluator(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    from zoneinfo import ZoneInfo

    from custom_components.ha_spark.transitions import TransitionRequest
    c = held_site
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={"committed_mode": "observe"}))
    c.hass.states.async_set("sensor.ev", "charging")
    inv = next(iter(c.native.inverters.values()))
    from custom_components.ha_spark.solis_frames import Reading
    c.native.readings[inv.id]["holding_43007"] = Reading(datetime.now(UTC), (190,))
    result = await c.control.async_request(TransitionRequest(target="protect"),
                                          now=datetime.now(UTC), zone=ZoneInfo("UTC"))
    assert result.refusal is None
    assert result.state is not None and result.state.transition is None
    assert result.state.holds[0].confirmed
    assert native_transport.holding[43007] == 222


async def test_failed_hold_save_prevents_off(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    monkeypatch.setattr(c.authority.store, "async_save", AsyncMock(side_effect=OSError()))
    await tick(c)
    assert c.authority.state is None
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_release_backoff_never_prevents_a_new_off(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    native_transport.holding.pop(43141)
    for _ in range(5):
        assert not await c.holds.async_release(datetime.now(UTC))
    native_transport.traffic.clear()
    assert not await c.holds.async_release(datetime.now(UTC))
    assert not native_transport.traffic
    # A foreign On is fresh polled evidence; Off never has a release backoff.
    native_transport.set_holding(43007, [190])
    from custom_components.ha_spark.solis_frames import Reading
    c.native.readings["inv1"]["holding_43007"] = Reading(datetime.now(UTC), (190,))
    await tick(c)
    assert native_transport.holding[43007] == 222


async def test_manual_release_in_optimise_preserves_owner_charging_edits(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    from custom_components.ha_spark.authority_store import ManualSession
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={"committed_mode": "optimise",
        "manual_session": ManualSession(return_to="optimise")}))
    native_transport.set_holding(43141, [120, 0, 1, 0, 2, 0] + [0] * 24)
    native_transport.traffic.clear()
    assert await c.holds.async_release(datetime.now(UTC))
    assert native_transport.holding[43141] == 120
    assert all(t.address == 43007 for t in native_transport.traffic if t.op == "write")


async def test_apply_failure_notice_survives_reload_and_repeats_only_once(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from homeassistant.components import persistent_notification
    c = held_site
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 86400}}})
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007, times=10)
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    assert notice.call_count == 1
    await c.authority.async_load()
    c.holds.notices.clear()  # restart loses only in-memory notification bookkeeping
    await tick(c)
    assert notice.call_count == 1
    await c.holds.async_reconcile(datetime.now(UTC) + timedelta(minutes=31))
    assert notice.call_count == 2
    assert c.authority.state is not None and c.authority.state.holds[0].apply_notice_repeated
    await c.authority.async_load()
    assert c.authority.state is not None and c.authority.state.holds[0].apply_notice_repeated
    c.holds.notices.clear()
    await c.holds.async_reconcile(datetime.now(UTC) + timedelta(minutes=62))
    assert [call.args[1] for call in notice.call_args_list] == [
        "A protection hold could not be applied."] * 2


async def test_new_session_waits_for_hold_write_and_keeps_verified_ownership(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    c = held_site
    started, finish = asyncio.Event(), asyncio.Event()
    original = native_transport.write
    async def write(address: int, values: list[int], *, device_id: int = 1) -> Any:
        started.set()
        await finish.wait()
        return await original(address, values, device_id=device_id)
    monkeypatch.setattr(native_transport, "write", write)
    c.hass.states.async_set("sensor.ev", "charging")
    hold = asyncio.create_task(c.holds.async_reconcile(datetime.now(UTC)))
    await started.wait()
    session = asyncio.create_task(c.control.async_new_session("replacement", now=datetime.now(UTC)))
    await asyncio.sleep(0)
    assert not session.done()
    finish.set()
    assert await hold
    assert await session
    await c.authority.async_load()
    state = c.authority.state
    assert state is not None and state.holds[0].confirmed
    assert state.addon_session_id == "replacement"


@pytest.mark.parametrize("first", ["ev", "dispatch"])
async def test_new_cause_under_confirmed_owned_off_survives_reload(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, first: str,
) -> None:
    c = held_site
    now = datetime.now(UTC)
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300},
        "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 300}}})
    dispatch = [{"start": now.isoformat(), "end": (now + timedelta(minutes=10)).isoformat()}]
    c.hass.states.async_set("sensor.ev", "charging" if first == "ev" else "stopped")
    c.hass.states.async_set("binary_sensor.dispatch", "on" if first == "dispatch" else "off",
                            {"planned_dispatches": dispatch if first == "dispatch" else []})
    await tick(c)
    assert native_transport.holding[43007] == 222
    native_transport.traffic.clear()
    c.hass.states.async_set("sensor.ev", "charging")
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": dispatch})
    await tick(c)
    await c.authority.async_load()
    state = c.authority.state
    assert state is not None and {h.cause for h in state.holds} == {"ev", "dispatch"}
    assert all(h.confirmed for h in state.holds)
    assert not any(t.op == "write" for t in native_transport.traffic)


async def test_real_removal_keeps_live_native_hold_and_reports_incomplete_handover(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from homeassistant.components import persistent_notification
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    notice = Mock()
    monkeypatch.setattr(persistent_notification, "async_create", notice)
    native_transport.traffic.clear()
    await c.hass.config_entries.async_remove(c.entry.entry_id)
    assert native_transport.holding[43007] == 222
    assert not any(t.op == "write" for t in native_transport.traffic)
    assert any(call.kwargs.get("title") == "ha-spark handover incomplete"
               for call in notice.call_args_list)


def _dispatch_site(c: SparkCoordinator, now: datetime) -> None:
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300},
        "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 300}}})
    c.hass.states.async_set("sensor.ev", "stopped")
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [{
        "start": now.isoformat(), "end": (now + timedelta(minutes=30)).isoformat()}]})


@pytest.mark.parametrize("change", ["source_removed", "attribute_missing"])
async def test_missing_dispatch_evidence_keeps_hold_until_trusted_end(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, freezer: Any, change: str,
) -> None:
    c = held_site
    _dispatch_site(c, datetime.now(UTC))
    await tick(c)
    assert native_transport.holding[43007] == 222
    freezer.tick(timedelta(minutes=1))
    if change == "source_removed":
        c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options, "evidence": {
            "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    else:
        c.hass.states.async_set("binary_sensor.dispatch", "on", {})
    c.hass.states.async_set("sensor.ev", "stopped", force_update=True)
    await tick(c)
    assert native_transport.holding[43007] == 222
    freezer.tick(timedelta(minutes=30))
    c.hass.states.async_set("sensor.ev", "stopped", force_update=True)
    await tick(c)
    assert native_transport.holding[43007] == 190


async def test_unknown_dispatch_evidence_rearms_an_unfinished_release(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, freezer: Any,
) -> None:
    c = held_site
    now = datetime.now(UTC)
    _dispatch_site(c, now)
    await tick(c)
    assert native_transport.holding[43007] == 222
    # Fresh clearance, but the restoration cannot verify: the release stays pending.
    c.hass.states.async_set("binary_sensor.dispatch", "off", {"planned_dispatches": []})
    saved = native_transport.holding.pop(43141)
    await tick(c)
    assert native_transport.holding[43007] == 222
    # Evidence goes stale before the trusted end; restoration recovers.
    native_transport.holding[43141] = saved
    freezer.tick(timedelta(minutes=10))
    c.hass.states.async_set("sensor.ev", "stopped", force_update=True)
    await tick(c)
    assert native_transport.holding[43007] == 222
    state = c.authority.state
    assert state is not None and state.holds and not state.holds[0].pending_release


async def test_retried_release_step_still_needs_fresh_clear_evidence(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await tick(c)
    state = c.authority.state
    assert state is not None
    # An unfinished removal handover persisted its release step.
    await c.authority.async_save(state.model_copy(update={"transition": Transition(
        target="observe", started_at=datetime.now(UTC), steps=("release_holds",))}))
    native_transport.traffic.clear()
    await c.control.async_retry()
    assert native_transport.holding[43007] == 222
    assert not any(t.op == "write" for t in native_transport.traffic)
    c.hass.states.async_set("sensor.ev", "stopped")
    await c.control.async_retry()
    assert native_transport.holding[43007] == 190


def _slot_one(window: list[int]) -> list[int]:
    """A 30-word 43141 frame with charge current 12 A and this Slot 1 window."""
    return [120, 0, *window] + [0] * 20


async def _manual_optimise(c: SparkCoordinator) -> None:
    state = c.authority.state
    assert state is not None
    await c.authority.async_save(state.model_copy(update={
        "committed_mode": "optimise",
        "manual_session": ManualSession(return_to="optimise"),
    }))


async def _restoration(c: SparkCoordinator, inverter_id: str, epoch: int) -> bool:
    c_guard = c.guard
    assert c_guard is not None
    inverter = c.native.inverters[inverter_id]
    async with c_guard.lock:
        async with c.native.lock:
            return await c.writes.async_restoration_held(inverter, epoch=epoch)


@pytest.mark.parametrize("trigger", ["resume", "expiry"])
async def test_manual_end_reverifies_the_owner_edited_slot_one_window(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice, trigger: str,
) -> None:
    c = held_site
    inverter = next(iter(c.native.inverters.values()))
    await _manual_optimise(c)
    state = c.authority.state
    assert state is not None
    old, edited = [23, 30, 5, 30, 0, 0, 0, 0], [22, 15, 6, 45, 0, 0, 0, 0]
    c.writes.verified_windows[inverter.id] = (state.epoch, tuple(old))
    c.writes.written_a[inverter.id] = 12.0
    native_transport.set_holding(43141, _slot_one(edited))
    native_transport.traffic.clear()
    now = datetime.now(UTC)
    if trigger == "expiry":
        assert await c.authority.async_save(state.model_copy(update={
            "manual_session": ManualSession(
                return_to="optimise", deadline=now - timedelta(seconds=1)),
        }))
        await c.control.async_expire(now=now, zone=ZoneInfo("UTC"))
        resumed = c.authority.state
    else:
        decision = await c.control.async_request(
            TransitionRequest(target="resume"), now=now, zone=ZoneInfo("UTC"))
        assert decision.refusal is None
        resumed = decision.state
    assert resumed is not None and resumed.manual_session is None
    assert resumed.committed_mode == "optimise"
    assert c.writes.last_apply is None  # no plan has been applied in this epoch
    assert c.writes.verified_windows[inverter.id] == (resumed.epoch, tuple(edited))
    assert await _restoration(c, inverter.id, resumed.epoch)


async def test_manual_end_leaves_a_discharge_half_unverified(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    inverter = next(iter(c.native.inverters.values()))
    await _manual_optimise(c)
    state = c.authority.state
    assert state is not None
    old = [23, 30, 5, 30, 0, 0, 0, 0]
    edited = [22, 15, 6, 45, 10, 0, 12, 0]
    c.writes.verified_windows[inverter.id] = (state.epoch, tuple(old))
    c.writes.written_a[inverter.id] = 12.0
    native_transport.set_holding(43141, _slot_one(edited))
    decision = await c.control.async_request(
        TransitionRequest(target="resume"), now=datetime.now(UTC), zone=ZoneInfo("UTC"))
    assert decision.state is not None
    assert c.writes.verified_windows.get(inverter.id) == (state.epoch, tuple(old))
    assert not await _restoration(c, inverter.id, decision.state.epoch)


async def test_manual_end_reverify_is_read_only(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    inverter = next(iter(c.native.inverters.values()))
    await _manual_optimise(c)
    window = [22, 15, 6, 45, 0, 0, 0, 0]
    native_transport.set_holding(43141, _slot_one(window))
    native_transport.traffic.clear()
    decision = await c.control.async_request(
        TransitionRequest(target="resume"), now=datetime.now(UTC), zone=ZoneInfo("UTC"))
    assert decision.state is not None
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert any(t.op == "read" and t.address == 43141 for t in native_transport.traffic)
    assert c.writes.verified_windows[inverter.id] == (decision.state.epoch, tuple(window))


async def test_resume_into_protect_records_no_window(
    held_site: SparkCoordinator, native_transport: FakeSolisDevice,
) -> None:
    c = held_site
    inverter = next(iter(c.native.inverters.values()))
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={
        "committed_mode": "protect",
        "manual_session": ManualSession(return_to="protect"),
    }))
    native_transport.set_holding(43141, _slot_one([22, 15, 6, 45, 0, 0, 0, 0]))
    c.writes.verified_windows.pop(inverter.id, None)
    decision = await c.control.async_request(
        TransitionRequest(target="resume"), now=datetime.now(UTC), zone=ZoneInfo("UTC"))
    assert decision.state is not None and decision.state.committed_mode == "protect"
    assert inverter.id not in c.writes.verified_windows
