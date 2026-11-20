"""Dated charge intents arm the charge half of Solis Slot 2 (#699, #700)."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.helpers import issue_registry as ir

from custom_components.ha_spark.authority_store import (
    HoldDevice,
    HoldRecord,
    ManualSession,
    Transition,
)
from custom_components.ha_spark.commands import (
    Command,
    DatedChargeNone,
    DatedChargeUntrusted,
    DatedChargeWindow,
)
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.solis_frames import Reading
from custom_components.ha_spark.transitions import Preconditions, TransitionRequest
from tests_ha.fake_solis import FakeSolisDevice, WriteFault
from tests_ha.test_native_export import (
    NOW,
    allow_export,
    apply_command,
    charge_intents,
    export_clock,
)
from tests_ha.test_native_write import command, hass_config_dir, hass_storage, writer

__all__ = ["export_clock", "hass_config_dir", "hass_storage", "writer"]

FIXTURE = (Path(__file__).parents[1]
           / "tests/fixtures/contract/command_apply_dated_charge_intents.json")
WINDOW = {"kind": "dated_charge", "request": "window", "current_a": 20.0,
          "start": "2026-10-24T12:00:00+01:00", "end": "2026-10-24T13:00:00+01:00"}


def test_integration_schema_accepts_the_add_on_golden() -> None:
    body = json.loads(FIXTURE.read_text())
    Command.model_validate(body)
    models: tuple[type[DatedChargeNone | DatedChargeUntrusted | DatedChargeWindow], ...] = (
        DatedChargeNone, DatedChargeUntrusted, DatedChargeWindow)
    for model, raw in zip(models, body["payload"]["intents"], strict=True):
        assert model.model_validate(raw).model_dump(mode="json") == raw


def apply(writer: SparkCoordinator, *intents: dict[str, Any]) -> Command:
    base = command(writer)
    inverter_id = base.payload["intents"][0]["inverter_id"]  # type: ignore[index,call-overload]
    return base.model_copy(update={"payload": {"intents": [
        base.payload["intents"][0],  # type: ignore[index]
        *({"inverter_id": inverter_id, **intent} for intent in intents),
    ]}})


def dated(start: datetime = NOW + timedelta(hours=1),
          end: datetime = NOW + timedelta(hours=2), **extra: Any) -> dict[str, Any]:
    return {"kind": "dated_charge", "inverter_id": "inv1", "request": "window",
            "current_a": 20.0, "start": start.isoformat(), "end": end.isoformat(), **extra}


def slot_writes(transport: FakeSolisDevice, address: int) -> list[tuple[int, ...]]:
    return [cast(tuple[int, ...], t.payload) for t in transport.traffic
            if t.op == "write" and t.address == address]


def records(c: SparkCoordinator) -> list[tuple[str, datetime, datetime, str]]:
    state = c.authority.state
    rows = state.dated_charges if state is not None else ()
    return [(row.inverter_id, row.start, row.end, row.state) for row in rows]


async def test_a_window_arms_the_charge_half_of_slot_2_only(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    native_transport.set_holding(43153, [0, 0, 0, 0, 3, 0, 4, 0])
    native_transport.set_holding(43141, [200])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), dated()]))
    assert [r.outcome for r in result.intents] == ["unchanged", "unchanged", "applied"]
    assert slot_writes(native_transport, 43153) == [(13, 0, 14, 0, 3, 0, 4, 0)]
    assert slot_writes(native_transport, 43143) == []
    assert records(writer) == [("inv1", NOW + timedelta(hours=1),
                                NOW + timedelta(hours=2), "armed")]


@pytest.mark.parametrize("mode", ["optimise", "protect", "observe"])
@pytest.mark.parametrize("request_", ["none", "untrusted"])
async def test_nothing_resident_and_nothing_wanted_is_unchanged(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, mode: str, request_: str,
) -> None:
    # Native apply acceptance needs every line verified, in every mode.
    allow_export(writer, monkeypatch, native_transport)
    state = writer.authority.state
    assert state is not None
    await writer.authority.async_save(state.model_copy(update={"committed_mode": mode}))
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": request_}]))
    assert result.intents[0].outcome == "unchanged"
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_window_for_a_later_day_is_deferred_and_writes_nothing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tomorrow's 12:30 face comes round today first: arming now would charge today.
    allow_export(writer, monkeypatch, native_transport)
    result = await writer._async_dispatch_command(apply_command(writer, [dated(
        NOW + timedelta(days=1, minutes=30), NOW + timedelta(days=1, hours=1, minutes=30))]))
    assert result.intents[0].outcome == "deferred"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert records(writer) == []


async def test_a_clock_change_night_is_judged_on_the_household_clock(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    # Fall-back night: 00:50Z is 01:50 BST, inside the local 01:30-02:00 face of a
    # 01:30Z-02:00Z (GMT) window, so arming it now would charge at once.
    now = datetime(2026, 10, 25, 0, 50, tzinfo=UTC)
    freezer.move_to(now)
    writer.control.grace_until = now - timedelta(seconds=1)
    allow_export(writer, monkeypatch, native_transport)
    local = now.astimezone(ZoneInfo("Europe/London"))
    registers = (local.year % 100, local.month, local.day, local.hour, local.minute, 0)
    native_transport.set_input(33022, list(registers))
    writer.native.readings["inv1"]["input_33022"] = Reading(now, registers, 0)
    result = await writer._async_dispatch_command(apply_command(writer, [dated(
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC), datetime(2026, 10, 25, 2, tzinfo=UTC))]))
    assert result.intents[0].outcome == "deferred"
    assert not [t for t in native_transport.traffic if t.op == "write"]


@pytest.mark.parametrize(("fence", "reason"), [
    ("observe", "mode"), ("protect", "mode"), ("fallback", "mode"), ("manual", "mode"),
    ("transition", "mode"), ("unknown", "authority_unknown"), ("epoch", "stale_epoch"),
    ("read_only", "read_only"), ("pending", "gateway_not_owned"), ("soc", "untrusted_soc"),
    ("clock", "clock_not_trusted"), ("hold", "hold_active"),
    ("power", "power_switch_not_on"), ("permission", "grid_charge_not_permitted"),
    ("ended", "invalid_intent"),
])
async def test_each_refusal_writes_nothing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fence: str, reason: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    state = writer.authority.state
    assert state is not None and writer.guard is not None
    inverter = writer.guard.topology.inverters[0]
    request = apply_command(writer, [dated()])
    intent = dated()
    if fence in ("observe", "protect"):
        await writer.authority.async_save(state.model_copy(update={"committed_mode": fence}))
    elif fence == "fallback":
        await writer.authority.async_save(state.model_copy(update={
            "fallback": state.fallback.model_copy(update={
                "active": True, "causes": ("addon_liveness",)})}))
    elif fence == "manual":
        await writer.authority.async_save(state.model_copy(update={
            "manual_session": ManualSession(return_to="optimise")}))
    elif fence == "transition":
        await writer.authority.async_save(state.model_copy(update={"transition": Transition(
            target="protect", started_at=NOW)}))
    elif fence == "unknown":
        monkeypatch.setattr(writer.authority, "status", "unknown")
    elif fence == "epoch":
        request = request.model_copy(update={"epoch": state.epoch + 1})
    elif fence == "read_only":
        native_transport.set_identity(model=0x27)
        await writer.async_reprobe(inverter.id)
    elif fence == "pending":
        monkeypatch.setattr(writer.control, "owned", lambda: False)
    elif fence == "soc":
        monkeypatch.setattr(writer, "soc", SimpleNamespace(
            increases_allowed=lambda bank: False), raising=False)
    elif fence == "clock":
        native_transport.set_input(33022, [26, 11, 2, 11, 0, 0])
        writer.native.readings["inv1"]["input_33022"] = Reading(NOW, (26, 11, 2, 11, 0, 0), 0)
    elif fence == "hold":
        assert await writer.authority.async_save(state.model_copy(update={"holds": (
            HoldRecord(cause="ev", started_at=NOW, confirmed=True, device=HoldDevice(
                driver="solis_native", power_switch=inverter.id,
                hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
                slave=inverter.gateway_unit_id)),)}))
    elif fence == "power":
        native_transport.set_holding(43007, [222])
    elif fence == "permission":
        native_transport.set_input(33132, [0])
    elif fence == "ended":
        intent = dated(NOW - timedelta(hours=2), NOW - timedelta(hours=1))
    request = request.model_copy(update={"payload": {"intents": [intent]}})
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(request)
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", reason)
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert records(writer) == []


async def test_a_read_back_mismatch_is_unverified_and_not_in_force(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.inject_write_fault(WriteFault("silent_noop"), address=43153)
    result = await writer._async_dispatch_command(apply_command(writer, [dated()]))
    assert result.intents[0].outcome == "unverified"
    assert [state for *_, state in records(writer)] == ["unknown"]


async def test_the_same_window_twice_is_unchanged_with_no_second_write(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    first = await writer._async_dispatch_command(apply_command(writer, [dated()]))
    assert first.intents[0].outcome == "applied"
    saved = writer.authority.state
    native_transport.traffic.clear()
    second = await writer._async_dispatch_command(apply_command(writer, [dated()], sequence=2))
    assert second.intents[0].outcome == "unchanged"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert writer.authority.state is saved


async def test_the_overnight_window_keeps_an_armed_slot_2(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43141, [200])
    command = apply_command(writer, [*charge_intents(), dated()])
    assert (await writer._async_dispatch_command(command)).intents[2].outcome == "applied"
    native_transport.traffic.clear()
    again = await writer._async_dispatch_command(command.model_copy(update={"sequence": 2}))
    assert [r.outcome for r in again.intents] == ["unchanged"] * 3
    assert not [t for t in native_transport.traffic if t.op == "write"]
    # A plan without the dated intent (an older add-on) still zeroes Slot 2.
    await writer._async_dispatch_command(apply_command(writer, charge_intents(), sequence=3))
    assert slot_writes(native_transport, 43153) == [(0,) * 8]


@pytest.mark.parametrize("how", ["restart", "reprobe"])
async def test_the_armed_record_survives_a_restart_and_a_reprobe(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, how: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    assert (await writer._async_dispatch_command(
        apply_command(writer, [dated()]))).intents[0].outcome == "applied"
    armed = records(writer)
    assert [state for *_, state in armed] == ["armed"]
    if how == "reprobe":
        await writer.async_reprobe("inv1")
        assert records(writer) == armed
        return
    entry = writer.entry
    assert await writer.hass.config_entries.async_reload(entry.entry_id)
    await writer.hass.async_block_till_done()
    assert records(entry.runtime_data) == armed


async def test_the_guard_clamps_the_daytime_current(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    assert writer.guard is not None
    # At 50 V and eta .949 this admits 20 whole DC amps.
    monkeypatch.setattr(writer.guard, "allowance_for", lambda bank: 1.005 / 0.949)
    native_transport.set_holding(43141, [0])
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "charge_current_limit", "inverter_id": "inv1", "current_a": 40.0},
        dated(current_a=40.0)]))
    assert [(r.outcome, r.written_a) for r in result.intents] == [
        ("applied", 20.0), ("applied", None)]
    assert slot_writes(native_transport, 43141) == [(200,)]


@pytest.mark.parametrize(("change", "reason"), [
    ("authority", "authority_unknown"), ("ownership", "gateway_not_owned"),
    ("limits", "limits"), ("hold", "hold_active"),
])
async def test_a_change_before_the_write_refuses_it(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, change: str, reason: str,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    assert writer.guard is not None
    inverter = writer.guard.topology.inverters[0]
    read = native_transport.read_holding
    slot2_reads = 0

    async def revoke(address: int, *args: Any, **kwargs: Any) -> Any:
        nonlocal slot2_reads
        result = await read(address, *args, **kwargs)
        slot2_reads += address == 43153
        if slot2_reads == 2:  # the write's own pre-read, after the record save
            state = writer.authority.state
            assert state is not None
            if change == "authority":
                monkeypatch.setattr(writer.authority, "status", "save_failed")
            elif change == "ownership":
                monkeypatch.setattr(writer.control, "owned", lambda: False)
            elif change == "limits":
                monkeypatch.setattr(writer.guard, "limits", object())
            else:
                await writer.authority.async_save(state.model_copy(update={"holds": (
                    HoldRecord(cause="ev", started_at=NOW, confirmed=True, device=HoldDevice(
                        driver="solis_native", power_switch=inverter.id,
                        hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
                        slave=inverter.gateway_unit_id)),)}))
        return result

    monkeypatch.setattr(native_transport, "read_holding", AsyncMock(side_effect=revoke))
    result = await writer._async_dispatch_command(apply_command(writer, [dated()]))
    assert (result.intents[0].outcome, result.intents[0].reason) == ("refused", reason)
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_a_hold_still_releases_over_an_armed_window_but_not_a_foreign_one(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allow_export(writer, monkeypatch, native_transport)
    native_transport.set_holding(43141, [200])
    result = await writer._async_dispatch_command(apply_command(
        writer, [*charge_intents(), dated()]))
    assert [r.outcome for r in result.intents][2] == "applied"
    state = writer.authority.state
    assert state is not None and writer.guard is not None
    inverter = writer.guard.topology.inverters[0]
    for reserved in (43151, 43161):  # read as part of the 30-register frame
        native_transport.set_holding(reserved, [0, 0])
    assert await writer.writes.async_restoration_held(inverter, epoch=state.epoch)
    native_transport.set_holding(43153, [13, 0, 15, 0, 0, 0, 0, 0])
    assert not await writer.writes.async_restoration_held(inverter, epoch=state.epoch)


@pytest.mark.parametrize("change", [
    {"start": "2026-10-24T12:00:00", "end": "2026-10-24T13:00:00"},
    {"start": "2026-10-24T13:00:00Z", "end": "2026-10-24T12:00:00Z"},
    {"current_a": 99.5},
    {"current_a": -1.0},
    {"request": "later"},
    {"surprise": "secret-value"},
])
async def test_a_malformed_dated_charge_is_refused_without_echo(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, change: dict[str, Any],
) -> None:
    result = await writer._async_dispatch_command(apply(writer, {**WINDOW, **change}))
    assert result.intents[0].outcome == "applied"
    assert (result.intents[1].outcome, result.intents[1].reason) == ("refused", "invalid_intent")
    text = result.model_dump_json()
    assert "secret-value" not in text and "99.5" not in text and "2026" not in text
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")


# --- #701 clearing the daytime window ------------------------------------------

def slot2_reads(transport: FakeSolisDevice) -> int:
    return len([t for t in transport.traffic if t.op == "read" and t.address == 43153])


async def armed(c: SparkCoordinator, transport: FakeSolisDevice,
                monkeypatch: pytest.MonkeyPatch, **window: Any) -> None:
    allow_export(c, monkeypatch, transport)
    transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])
    transport.set_holding(43153, [0, 0, 0, 0, 3, 0, 4, 0])
    result = await c._async_dispatch_command(apply_command(c, [dated(**window)]))
    assert result.intents[0].outcome == "applied"
    transport.traffic.clear()


async def test_none_clears_an_armed_window_and_confirms_the_zero(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    result = await writer._async_dispatch_command(apply_command(writer, [
        {"kind": "dated_charge", "inverter_id": "inv1", "request": "none"}], sequence=2))
    assert result.intents[0].outcome == "applied"
    assert slot_writes(native_transport, 43153) == [(0, 0, 0, 0, 3, 0, 4, 0)]
    assert slot_writes(native_transport, 43143) == []
    # Fresh read, verify read, then five further matching zero reads.
    assert slot2_reads(native_transport) >= 7
    assert records(writer) == []


def request(kind: str) -> dict[str, Any]:
    return {"kind": "dated_charge", "inverter_id": "inv1", "request": kind}


async def test_untrusted_keeps_a_verified_window_for_a_session_under_way(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory,
) -> None:
    await armed(writer, native_transport, monkeypatch, start=NOW - timedelta(minutes=30))
    freezer.move_to(NOW + timedelta(minutes=10))
    result = await writer._async_dispatch_command(apply_command(
        writer, [request("untrusted")], sequence=2))
    assert result.intents[0].outcome == "unchanged"
    assert not [t for t in native_transport.traffic if t.op == "write"]
    assert [state for *_, state in records(writer)] == ["armed"]


@pytest.mark.parametrize("resident", ["foreign", "moved"])
async def test_untrusted_clears_a_window_it_cannot_verify(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, resident: str,
) -> None:
    if resident == "foreign":
        allow_export(writer, monkeypatch, native_transport)
        native_transport.set_holding(43153, [13, 0, 15, 0, 0, 0, 0, 0])
    else:
        await armed(writer, native_transport, monkeypatch)
        native_transport.set_holding(43153, [13, 0, 15, 0, 0, 0, 0, 0])
    native_transport.traffic.clear()
    result = await writer._async_dispatch_command(apply_command(
        writer, [request("untrusted")], sequence=2))
    assert result.intents[0].outcome == "applied"
    assert slot_writes(native_transport, 43153) == [(0,) * 8]
    assert records(writer) == []


@pytest.mark.parametrize("how", ["running", "restart"])
async def test_the_tick_clears_the_window_at_its_end(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, freezer: FrozenDateTimeFactory, how: str,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    c = writer
    if how == "restart":
        # The add-on never calls again: the durable record alone drives the clear.
        entry = writer.entry
        assert await writer.hass.config_entries.async_reload(entry.entry_id)
        await writer.hass.async_block_till_done()
        c = entry.runtime_data
        c.control.grace_until = NOW - timedelta(seconds=1)
    native_transport.traffic.clear()
    await c.writes.async_retry_dated(NOW + timedelta(hours=1, minutes=59))
    assert not [t for t in native_transport.traffic if t.op == "write"]
    end = NOW + timedelta(hours=2)
    freezer.move_to(end)
    await c.writes.async_retry_dated(end)
    assert slot_writes(native_transport, 43153) == [(0, 0, 0, 0, 3, 0, 4, 0)]
    assert slot_writes(native_transport, 43143) == []
    assert slot2_reads(native_transport) >= 7
    assert records(c) == []


@pytest.mark.parametrize("target", ["protect", "observe", "manual"])
async def test_leaving_optimise_clears_the_window_in_the_transition(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    writer.control.preconditions = lambda: Preconditions(True, True, True)
    writer.control.grace_until = NOW - timedelta(seconds=1)
    decision = await writer.control.async_request(
        TransitionRequest(target=target), now=NOW, zone=ZoneInfo("UTC"))  # type: ignore[arg-type]
    assert decision.refusal is None
    assert records(writer) == []
    assert slot_writes(native_transport, 43153)[-1][:4] == (0, 0, 0, 0)
    assert slot2_reads(native_transport) >= 7
    state = writer.authority.state
    assert state is not None and "cancel_export" not in (
        state.transition.steps if state.transition is not None else ())


def dated_issues(c: SparkCoordinator) -> list[str]:
    return [issue_id for (domain, issue_id) in ir.async_get(c.hass).issues
            if domain == "ha_spark" and issue_id.startswith("dated_clear_failed_")]


@pytest.mark.parametrize("fault", ["readback", "confirm"])
async def test_a_failed_clear_keeps_the_record_retries_and_raises_a_repair(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    if fault == "readback":
        native_transport.inject_write_fault(WriteFault("silent_noop"), address=43153)
    else:
        read = native_transport.read_holding
        reads = 0

        async def flicker(address: int, *args: Any, **kwargs: Any) -> Any:
            nonlocal reads
            result = await read(address, *args, **kwargs)
            reads += address == 43153
            if reads == 4:  # the window comes back during the five further zero reads
                native_transport.set_holding(43153, [13, 0, 14, 0, 3, 0, 4, 0])
            return result

        monkeypatch.setattr(native_transport, "read_holding", AsyncMock(side_effect=flicker))
    result = await writer._async_dispatch_command(apply_command(
        writer, [request("none")], sequence=2))
    assert result.intents[0].outcome == "unverified"
    assert [state for *_, state in records(writer)] == ["clear_pending"]
    assert len(dated_issues(writer)) == 1
    native_transport.__dict__.pop("read_holding", None)
    native_transport.set_holding(43153, [13, 0, 14, 0, 3, 0, 4, 0])
    # The window is still ahead, but the clear obligation is the tick's to retry.
    await writer.writes.async_retry_dated(NOW)
    assert records(writer) == []
    assert slot_writes(native_transport, 43153)[-1] == (0, 0, 0, 0, 3, 0, 4, 0)
    assert dated_issues(writer) == []


async def test_dated_and_export_clears_leave_each_other_alone(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    state = writer.authority.state
    assert state is not None
    async with writer.control.lock:
        result = await writer.writes.async_clear_export("inv1", epoch=state.epoch, cleanup=True)
    assert result.outcome == "applied"
    assert slot_writes(native_transport, 43153) == []
    assert [state for *_, state in records(writer)] == ["armed"]
    native_transport.set_holding(43143, [23, 30, 5, 30, 14, 0, 15, 0])
    native_transport.traffic.clear()
    await writer._async_dispatch_command(apply_command(writer, [request("none")], sequence=2))
    assert records(writer) == []
    assert slot_writes(native_transport, 43143) == []


async def test_rearming_the_same_window_in_a_new_epoch_restamps_the_record(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await armed(writer, native_transport, monkeypatch)
    state = writer.authority.state
    assert state is not None
    # A failed clear left the record behind, then the site came back to Optimise.
    [record] = state.dated_charges
    await writer.authority.async_save(state.model_copy(update={
        "epoch": state.epoch + 1,
        "dated_charges": (record.model_copy(update={"state": "clear_pending"}),)}))
    native_transport.set_holding(43153, [0, 0, 0, 0, 3, 0, 4, 0])
    result = await writer._async_dispatch_command(apply_command(
        writer, [dated()], sequence=2))
    assert result.intents[0].outcome == "applied"
    state = writer.authority.state
    assert state is not None
    [record] = state.dated_charges
    assert (record.state, record.epoch) == ("armed", state.epoch)
    native_transport.traffic.clear()
    await writer.writes.async_retry_dated(NOW)
    assert not [t for t in native_transport.traffic if t.op == "write"]


async def test_untrusted_clears_an_armed_window_that_has_not_started(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A booking that goes stale before it starts no longer justifies the window.
    await armed(writer, native_transport, monkeypatch)
    result = await writer._async_dispatch_command(apply_command(
        writer, [request("untrusted")], sequence=2))
    assert result.intents[0].outcome == "applied"
    assert slot_writes(native_transport, 43153) == [(0, 0, 0, 0, 3, 0, 4, 0)]
    assert records(writer) == []
