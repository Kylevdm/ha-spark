"""A dispatch hold hands its power switch to an armed purchase window (#769)."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_spark import native_power
from custom_components.ha_spark.authority_store import HoldRecord, Transition
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_hold import hold_device
from custom_components.ha_spark.solis_frames import Reading
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_dated_charge import dated, records
from tests_ha.test_native_export import NOW, allow_export, apply_command, export_clock
from tests_ha.test_native_write import hass_config_dir, hass_storage, writer
from tests_ha.test_topology_flow import choose

__all__ = ["export_clock", "hass_config_dir", "hass_storage", "writer"]

OFF, ON = 222, 190
END = NOW + timedelta(minutes=30)
DISPATCH_END = NOW + timedelta(hours=3)


def window(**extra: Any) -> dict[str, Any]:
    return dated(NOW, END, **({"target_soc_pct": 80.0} | extra))


def switch_writes(transport: FakeSolisDevice) -> list[int]:
    return [t.payload[0] for t in transport.traffic  # type: ignore[index]
            if t.op == "write" and t.address == 43007]


def handed(c: SparkCoordinator) -> tuple[datetime, float] | None:
    handover = c.writes.handovers.get("inv1")
    return None if handover is None else (handover.end, handover.target_soc_pct)


def reclaim_issues(c: SparkCoordinator) -> list[str]:
    return [issue_id for (domain, issue_id) in ir.async_get(c.hass).issues
            if domain == "ha_spark" and issue_id.startswith("hold_reclaim_failed_")]


def report_dispatch(c: SparkCoordinator, end: datetime | None = DISPATCH_END) -> None:
    dispatches = [] if end is None else [{
        "start": (NOW - timedelta(minutes=10)).isoformat(), "end": end.isoformat(),
    }]
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": dispatches})


def current(amps: float) -> dict[str, Any]:
    return {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": amps}


def set_clock(c: SparkCoordinator, now: datetime, *, skew: int = 0) -> None:
    face = now + timedelta(seconds=skew)
    clock = (face.year % 100, face.month, face.day, face.hour, face.minute, face.second)
    inverter = c.native.inverters["inv1"]
    transport = c.native.transport(inverter)
    if isinstance(transport, FakeSolisDevice):
        # A real native poll during the test reads the device clock too.
        transport.set_input(33022, list(clock))
    c.native.readings[inverter.id]["input_33022"] = Reading(
        now, clock, transport.connection_epoch)


async def held(c: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
               transport: FakeSolisDevice, *, opt_in: bool = True,
               causes: tuple[str, ...] = ("dispatch", "ev"),
               verify_current: bool = True) -> SimpleNamespace:
    """Optimise, a confirmed owned hold with the switch Off, a live dispatch."""
    async def sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(native_power, "verify_sleep", sleep)
    allow_export(c, monkeypatch, transport, power=OFF)
    # The shared native writer fixture models a v4 peer; handover needs a counted v6 exchange.
    monkeypatch.setattr(c, "counted_api_version", 6)
    bank = SimpleNamespace(value=50.0, trust="trusted", reason="ok", read_at=NOW)
    monkeypatch.setattr(c, "soc", SimpleNamespace(
        trusted=lambda b: bank.trust == "trusted",
        increases_allowed=lambda b: bank.trust == "trusted",
        bank=lambda b: bank, snapshot=lambda: [], observe=lambda now: None,
        fallback_requested=False, recovery_ready=False))
    c.hass.states.async_set("sensor.ev", "charging")
    report_dispatch(c)
    c.hass.config_entries.async_update_entry(c.entry, options={
        **c.entry.options, "dispatch_handover": opt_in})
    assert c.guard is not None
    device = hold_device(c.guard.topology.inverters[0])
    holds = tuple(
        HoldRecord(cause="dispatch", started_at=NOW, confirmed=True, device=device,
                   trusted_end=DISPATCH_END) if cause == "dispatch"
        else HoldRecord(cause="ev", started_at=NOW, confirmed=True, device=device)
        for cause in causes)
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={"holds": holds}))
    if verify_current:
        result = await c._async_dispatch_command(apply_command(c, [current(10.0)]))
        assert result.intents[0].outcome == "unchanged"
    transport.traffic.clear()
    return bank


async def hand_over(c: SparkCoordinator, transport: FakeSolisDevice, *,
                    sequence: int = 1) -> str:
    result = await c._async_dispatch_command(apply_command(c, [window()], sequence=sequence))
    return result.intents[0].outcome


async def tick(c: SparkCoordinator) -> None:
    await c.holds.async_reconcile(datetime.now(UTC))


async def test_the_hold_hands_over_only_after_the_window_is_armed_and_read_back(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    original = native_transport.write

    async def write(address: int, values: list[int], *, device_id: int = 1) -> Any:
        if address == 43007:
            # The window is armed and recorded before On.
            assert [state for *_, state in records(writer)] == ["armed"]
            assert handed(writer) == (END, 80.0)
        return await original(address, values, device_id=device_id)

    monkeypatch.setattr(native_transport, "write", write)
    assert await hand_over(writer, native_transport) == "applied"
    assert [t.address for t in native_transport.traffic if t.op == "write"] == [43153, 43007]
    assert native_transport.holding[43007] == ON


@pytest.mark.parametrize("case", ["opt_out", "no_target", "ev_only", "near_target", "margin",
                                  "untrusted", "counted_v5", "not_counted", "zero_current"])
async def test_without_every_condition_today_s_refusal_stands(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, case: str,
) -> None:
    bank = await held(writer, monkeypatch, native_transport, opt_in=case != "opt_out",
                      causes=("ev",) if case == "ev_only" else ("dispatch", "ev"))
    intent = window()
    if case == "no_target":
        intent.pop("target_soc_pct")
    elif case == "near_target":
        bank.value = 78.0
    elif case == "margin":
        intent = dated(NOW, NOW + timedelta(minutes=2), target_soc_pct=80.0)
    elif case == "untrusted":
        bank.trust = "untrusted"
    elif case == "counted_v5":
        writer.counted_api_version = 5
    elif case == "not_counted":
        # Proposed or advertised versions are not a counted exchange.
        writer.api_version = writer.addon_api_version = 6
        writer.counted_api_version = None
    elif case == "zero_current":
        # Verified positive, then a later poll reads zero: no On on that proof.
        freezer.tick(timedelta(seconds=1))
        inverter = writer.native.inverters["inv1"]
        writer.native.readings["inv1"]["holding_43141"] = Reading(
            datetime.now(UTC), (0,) * 30, writer.native.transport(inverter).connection_epoch)
    result = await writer._async_dispatch_command(apply_command(writer, [intent]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "hold_active")
    assert switch_writes(native_transport) == []
    assert handed(writer) is None


async def test_a_failed_arm_hands_nothing_over(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43153)
    assert await hand_over(writer, native_transport) == "unverified"
    assert switch_writes(native_transport) == []
    assert handed(writer) is None


async def test_an_unverified_on_keeps_an_off_to_verify(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    result = await writer._async_dispatch_command(apply_command(writer, [window()]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "hold_active")
    # The On may have reached the inverter: the handover stays until Off is verified.
    assert handed(writer) == (END, 80.0)
    await tick(writer)
    assert native_transport.holding[43007] == OFF
    assert handed(writer) is None


async def test_the_same_window_again_is_unchanged_and_the_tick_leaves_it_on(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.traffic.clear()
    assert await hand_over(writer, native_transport, sequence=2) == "unchanged"
    await tick(writer)
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert native_transport.holding[43007] == ON
    state = writer.authority.state
    assert state is not None and all(h.confirmed for h in state.holds)


@pytest.mark.parametrize("cause", ["margin", "target", "untrusted", "counted_v5", "protect",
                                   "window_cleared", "cancelled", "dispatch_untrusted"])
async def test_the_hold_takes_back_and_never_hands_the_same_window_over_again(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, cause: str,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    if cause == "margin":
        freezer.move_to(END - timedelta(minutes=2))
        set_clock(writer, datetime.now(UTC))
        report_dispatch(writer)
    elif cause == "target":
        bank.value = 78.0
    elif cause == "untrusted":
        bank.trust = "untrusted"
    elif cause == "counted_v5":
        writer.counted_api_version = 5
    elif cause == "protect":
        state = writer.authority.state
        assert state is not None
        await writer.authority.async_save(state.model_copy(update={"committed_mode": "protect"}))
    elif cause == "window_cleared":
        state = writer.authority.state
        assert state is not None
        await writer.authority.async_save(state.model_copy(update={"dated_charges": ()}))
    elif cause == "cancelled":
        # The car still charges, so the EV hold is still wanted.
        report_dispatch(writer, None)
    else:
        writer.hass.states.async_set("binary_sensor.dispatch", "unavailable")
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]
    assert handed(writer) is None
    assert END in writer.writes.reclaimed["inv1"]
    if cause == "target":
        # Back under target, the same window is never handed over again.
        bank.value = 50.0
        result = await writer._async_dispatch_command(apply_command(
            writer, [window()], sequence=2))
        assert (result.intents[0].outcome, result.intents[0].reason) == (
            "refused", "hold_active")
        assert native_transport.holding[43007] == OFF


async def test_trusted_cancellation_without_another_hold_releases_normally(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport, causes=("dispatch",))
    assert await hand_over(writer, native_transport) == "applied"
    report_dispatch(writer, None)
    writer.hass.states.async_set("sensor.ev", "stopped")
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == []
    assert native_transport.holding[43007] == ON
    assert handed(writer) is None


async def test_a_failed_take_back_retries_raises_a_repair_and_notifies(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    notices: list[str] = []
    monkeypatch.setattr(
        "custom_components.ha_spark.notify_target.async_notice",
        lambda hass, entry, **kwargs: notices.append(kwargs["message"]))
    bank.trust = "untrusted"
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    await tick(writer)
    assert native_transport.holding[43007] == ON
    assert len(reclaim_issues(writer)) == 1
    assert notices == ["A protection hold could not be applied."]
    bank.trust = "trusted"
    await tick(writer)
    assert native_transport.holding[43007] == OFF
    assert reclaim_issues(writer) == []


async def test_a_restart_forgets_the_handover_and_the_first_tick_turns_off(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    entry = writer.entry
    assert await writer.hass.config_entries.async_reload(entry.entry_id)
    await writer.hass.async_block_till_done()
    c: SparkCoordinator = entry.runtime_data
    assert c.writes.handovers == {}
    await tick(c)
    assert native_transport.holding[43007] == OFF


async def test_a_live_handover_saves_a_newly_wanted_cause(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `held` reports the car charging; only the dispatch hold is recorded.
    await held(writer, monkeypatch, native_transport, causes=("dispatch",))
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.traffic.clear()
    await tick(writer)
    state = writer.authority.state
    assert state is not None
    assert sorted(h.cause for h in state.holds) == ["dispatch", "ev"]
    assert all(h.confirmed for h in state.holds)
    assert switch_writes(native_transport) == []


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_owner_opt_in_is_off_by_default_and_saved_from_the_options(
    writer: SparkCoordinator, enabled: bool,
) -> None:
    assert "dispatch_handover" not in writer.entry.options
    result = await writer.hass.config_entries.options.async_init(writer.entry.entry_id)
    assert "dispatch_handover" in result["menu_options"]
    result = await choose(writer.hass, result["flow_id"], "dispatch_handover")
    assert result["step_id"] == "dispatch_handover"
    with patch.object(writer.hass.config_entries, "async_reload", return_value=True):
        result = await writer.hass.config_entries.options.async_configure(
            result["flow_id"], {"dispatch_handover": enabled})
        await writer.hass.async_block_till_done()
    assert writer.entry.options["dispatch_handover"] is enabled


@pytest.mark.parametrize("skew", [-30, 61, -61, None])
async def test_start_requires_the_inverter_to_be_inside_the_window_with_a_trusted_clock(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, skew: int | None,
) -> None:
    await held(writer, monkeypatch, native_transport)
    set_clock(writer, NOW if skew is not None else NOW - timedelta(seconds=61), skew=skew or 0)
    assert await hand_over(writer, native_transport) == "refused"
    assert switch_writes(native_transport) == []
    if skew == -30:
        freezer.move_to(NOW + timedelta(seconds=30))
        assert await hand_over(writer, native_transport, sequence=2) == "applied"
        assert switch_writes(native_transport) == [ON]


async def test_ordinary_dated_window_keeps_the_five_minute_clock_tolerance(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport, clock_offset=timedelta(minutes=5))
    assert await hand_over(writer, native_transport) == "applied"
    assert records(writer) == [("inv1", NOW, END, "armed")]
    assert switch_writes(native_transport) == []


@pytest.mark.parametrize("skew", [-60, 60])
async def test_take_back_margin_follows_the_inverter_clock(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, skew: int,
) -> None:
    await held(writer, monkeypatch, native_transport)
    monkeypatch.setattr(writer.fallback, "async_evaluate", AsyncMock())
    assert await hand_over(writer, native_transport) == "applied"
    deadline = END - timedelta(minutes=2, seconds=skew)
    freezer.move_to(deadline - timedelta(seconds=1))
    set_clock(writer, datetime.now(UTC), skew=skew)
    report_dispatch(writer)
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == []
    freezer.move_to(deadline)
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


@pytest.mark.parametrize("words", ["zero_current", "other_face"])
async def test_a_newer_contradicting_charge_block_poll_ends_the_handover(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory, words: str,
) -> None:
    await held(writer, monkeypatch, native_transport)
    monkeypatch.setattr(writer.fallback, "async_evaluate", AsyncMock())
    assert await hand_over(writer, native_transport) == "applied"
    freezer.tick(timedelta(seconds=5))
    inverter = writer.native.inverters["inv1"]
    block = ((0,) * 30 if words == "zero_current"
             else (100,) + (0,) * 11 + (1, 2, 3, 4) + (0,) * 14)
    writer.native.readings["inv1"]["holding_43141"] = Reading(
        datetime.now(UTC), block, writer.native.transport(inverter).connection_epoch)
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


@pytest.mark.parametrize("path", ["none", "retry", "transition", "fallback", "ordinary"])
async def test_any_slot_2_change_under_a_handover_turns_off_first(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, path: str,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.traffic.clear()
    state = writer.authority.state
    assert state is not None
    if path == "none":
        result = await writer._async_dispatch_command(apply_command(writer, [
            {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=2))
        assert result.intents[0].outcome == "applied"
    elif path == "retry":
        freezer.move_to(END)
        report_dispatch(writer)
        await writer.writes.async_retry_dated(END)
    elif path == "transition":
        assert await writer.authority.async_save(state.model_copy(update={"transition": Transition(
            target="protect", started_at=NOW, steps=("cancel_export",))}))
        async with writer.control.lock:
            await writer.writes.async_cancel_export("inv1", epoch=state.epoch)
    elif path == "fallback":
        assert await writer.authority.async_save(state.model_copy(update={
            "fallback": state.fallback.model_copy(update={
                "active": True, "causes": ("soc_integrity",), "started_at": NOW})}))
        async with writer.control.lock:
            await writer.writes.async_fallback(
                "inv1", current_a=10, start="01:00", end="03:00", epoch=state.epoch)
    else:
        await writer._async_dispatch_command(apply_command(writer, [{
            "kind": "charge_window", "inverter_id": "inv1", "current_a": 10,
            "start": "01:00", "end": "03:00"}], sequence=2))
    writes = [t.address for t in native_transport.traffic if t.op == "write"]
    assert writes.index(43007) < writes.index(43153)
    assert native_transport.holding[43007] == OFF
    assert not any(native_transport.holding[reg] for reg in range(43153, 43157))
    assert handed(writer) is None


async def test_a_move_to_observe_clears_slot_2_without_a_new_off(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The owner is giving protection up: the hold gate toward Observe is not bypassed.
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.traffic.clear()
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={"transition": Transition(
        target="observe", started_at=NOW, steps=("cancel_export", "release_holds"))}))
    async with writer.control.lock:
        await writer.writes.async_cancel_export("inv1", epoch=state.epoch)
    assert switch_writes(native_transport) == []
    assert not any(native_transport.holding[reg] for reg in range(43153, 43157))


async def test_a_failed_take_back_keeps_slot_2_until_the_switch_is_off(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # While Off fails and the hold stands, the window is its only protection.
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=2))
    assert result.intents[0].outcome == "failed"
    assert native_transport.holding[43007] == ON
    assert any(native_transport.holding[reg] for reg in range(43153, 43157))
    assert len(reclaim_issues(writer)) == 1
    await tick(writer)
    assert native_transport.holding[43007] == OFF
    assert reclaim_issues(writer) == []
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=3))
    assert result.intents[0].outcome == "applied"
    assert records(writer) == []


async def test_an_off_seen_under_a_handover_is_its_take_back(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.holding[43007] = OFF  # the owner turns it off before the hold tick
    native_transport.traffic.clear()
    assert await hand_over(writer, native_transport, sequence=2) == "refused"
    assert switch_writes(native_transport) == []
    assert handed(writer) is None


async def test_the_same_window_under_a_new_plan_takes_its_new_target(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    bank.value = 75.0
    assert await hand_over(writer, native_transport) == "applied"
    result = await writer._async_dispatch_command(apply_command(
        writer, [window(target_soc_pct=76.0)], sequence=2))
    # 75 % is within 2 % of the new target: no new handover, and the live one ends.
    assert result.intents[0].outcome == "refused"
    assert handed(writer) == (END, 76.0)
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


async def test_a_released_hold_leaves_no_take_back(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    state = writer.authority.state
    assert state is not None
    # A transition's own release retired the records; the handover is stale.
    assert await writer.authority.async_save(state.model_copy(update={"holds": ()}))
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=2))
    assert result.intents[0].outcome == "applied"
    assert switch_writes(native_transport) == []
    assert handed(writer) is None


async def test_a_zero_current_write_takes_the_switch_back_first(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.traffic.clear()
    await writer._async_dispatch_command(apply_command(writer, [current(0.0)], sequence=2))
    writes = [t.address for t in native_transport.traffic if t.op == "write"]
    assert writes.index(43007) < writes.index(43141)
    assert native_transport.holding[43007] == OFF


async def test_a_window_longer_than_a_half_hour_is_never_handed_over(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    result = await writer._async_dispatch_command(apply_command(writer, [dated(
        NOW, NOW + timedelta(days=1, minutes=30), target_soc_pct=80.0)]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", "hold_active")
    assert switch_writes(native_transport) == []


async def test_a_poll_showing_grid_charge_not_permitted_ends_the_handover(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    await held(writer, monkeypatch, native_transport)
    monkeypatch.setattr(writer.fallback, "async_evaluate", AsyncMock())
    assert await hand_over(writer, native_transport) == "applied"
    freezer.tick(timedelta(seconds=5))
    inverter = writer.native.inverters["inv1"]
    writer.native.readings["inv1"]["input_33132"] = Reading(
        datetime.now(UTC), (0,) * 21, writer.native.transport(inverter).connection_epoch)
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


async def test_every_taken_back_window_is_remembered(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    other_end = END - timedelta(minutes=10)
    for sequence, intent in ((1, window()), (2, dated(NOW, other_end, target_soc_pct=80.0))):
        bank.value = 50.0
        result = await writer._async_dispatch_command(apply_command(
            writer, [intent], sequence=sequence))
        assert result.intents[0].outcome == "applied"
        bank.value = 78.0
        await tick(writer)
        assert native_transport.holding[43007] == OFF
    bank.value = 50.0
    native_transport.traffic.clear()
    assert await hand_over(writer, native_transport, sequence=3) == "refused"
    assert switch_writes(native_transport) == []


async def test_a_cached_off_from_before_the_on_is_not_a_take_back(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    inverter = writer.native.inverters["inv1"]
    writer.native.readings["inv1"]["holding_43007"] = Reading(
        datetime.now(UTC), (OFF,), writer.native.transport(inverter).connection_epoch)
    await tick(writer)
    # The switch was read fresh and the live handover kept.
    assert native_transport.holding[43007] == ON
    assert handed(writer) == (END, 80.0)


async def test_a_failed_take_back_is_owed_until_an_off_is_verified(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=2))
    assert result.intents[0].outcome == "failed"
    # Every condition still holds, yet the failed Off is owed: the tick writes Off.
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]
    assert handed(writer) is None


async def test_a_plan_zero_current_waits_for_a_failed_take_back(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43007)
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [current(0.0)], sequence=2))
    assert result.intents[0].outcome == "failed"
    assert 43141 not in [t.address for t in native_transport.traffic if t.op == "write"]


async def test_a_later_start_for_the_same_purchase_takes_the_new_target(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    monkeypatch.setattr(writer.fallback, "async_evaluate", AsyncMock())
    bank.value = 75.0
    assert await hand_over(writer, native_transport) == "applied"
    freezer.tick(timedelta(minutes=5))
    set_clock(writer, datetime.now(UTC))
    await writer._async_dispatch_command(apply_command(writer, [dated(
        NOW + timedelta(minutes=5), END, target_soc_pct=76.0)], sequence=2))
    assert handed(writer) == (END, 76.0)
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


async def test_a_direct_read_without_grid_charge_permission_ends_the_handover(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await held(writer, monkeypatch, native_transport)
    assert await hand_over(writer, native_transport) == "applied"
    native_transport.set_input(33132, [0])
    result = await writer._async_dispatch_command(apply_command(writer, [window()], sequence=2))
    assert result.intents[0].reason == "grid_charge_not_permitted"
    native_transport.traffic.clear()
    await tick(writer)
    assert switch_writes(native_transport) == [OFF]


async def test_the_latch_is_the_slot_2_minute_not_the_second(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, monkeypatch: pytest.MonkeyPatch,
    freezer: FrozenDateTimeFactory,
) -> None:
    bank = await held(writer, monkeypatch, native_transport)
    monkeypatch.setattr(writer.fallback, "async_evaluate", AsyncMock())
    assert await hand_over(writer, native_transport) == "applied"
    bank.value = 78.0
    await tick(writer)
    bank.value = 50.0
    freezer.tick(timedelta(seconds=30))
    set_clock(writer, datetime.now(UTC))
    native_transport.traffic.clear()
    # Same Slot 2 face (12:00-12:30), timestamps 30 s later.
    shift = timedelta(seconds=30)
    result = await writer._async_dispatch_command(apply_command(writer, [dated(
        NOW + shift, END + shift, target_soc_pct=80.0)], sequence=2))
    assert result.intents[0].outcome == "refused"
    assert switch_writes(native_transport) == []
