"""#428: during a transition only the running handover step writes, and the
→ Observe release hands ha-spark's own holds back through the release order."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.authority import HoldDevice, HoldRecord, Transition
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.devices.outcomes import ActionOutcome, is_verified
from ha_spark.energy.export_store import ExportEventStore
from tests.test_solis_power_switch import (
    _SWITCH,
    FakeRest,
    _device,
    _intent,
    _now,
    _options,
    _window,
)

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)


def _hold(device: SolisDevice, *, confirmed: bool = True) -> HoldRecord:
    return HoldRecord(
        cause="ev", started_at=datetime.now(UTC) - timedelta(hours=1), confirmed=confirmed,
        device=HoldDevice(
            driver="solis", power_switch=_SWITCH, hub=device._settings.solis_control_hub,
            slave=device._settings.solis_modbus_slave,
        ),
    )


def _in_transition(
    device: SolisDevice, steps: tuple[str, ...], *, holds: tuple[HoldRecord, ...] = (),
    target: str = "observe",
) -> None:
    """Persist a transition with a bumped epoch and plan under it, as the runner will."""
    store = device._execution.authority
    assert store is not None and store.state is not None
    epoch = store.state.epoch + 1
    store.save(store.state.model_copy(update={
        "holds": holds, "epoch": epoch,
        "transition": Transition(target=target, started_at=datetime.now(UTC), steps=steps),
    }))
    device._execution.planned_epoch.set(epoch)


def _protect_device(rest: FakeRest, tmp_path) -> SolisDevice:
    # A readable resident slot-1 window and charge current.
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "23"
    rest.states["sensor.solis_control_timed_charge_start_minutes"] = "30"
    rest.states["sensor.solis_control_timed_charge_end_hours"] = "5"
    rest.states["sensor.solis_control_timed_charge_end_minutes"] = "30"
    rest.states["sensor.solis_control_timed_charge_current"] = "40"
    return _device(rest, tmp_path, authority_mode="protect")


async def test_release_step_turns_our_confirmed_hold_on_then_clears_it(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(_hold(device),))

    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert _options(rest) == ["On"]
    assert [line.outcome for line in lines] == [ActionOutcome.VERIFIED]
    store = device._execution.authority
    assert store is not None and store.state is not None and store.state.holds == ()


async def test_release_outside_its_step_writes_nothing(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(_hold(device),))

    for step in (None, "commit_observe", "evaluate_protection"):
        if step is None:
            lines = await device.release_holds()
        else:
            with device._execution.handover(step):
                lines = await device.release_holds()
        assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert _options(rest) == []


async def test_handover_step_never_grants_charging(tmp_path) -> None:
    rest = FakeRest(switch="On")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"))

    with device._execution.handover("release_holds"):
        line = await device.set_charge_rate(1000)

    assert line.outcome is ActionOutcome.REFUSED
    assert rest.calls == []


async def test_handover_step_still_checks_the_planned_epoch(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(_hold(device),))
    device._execution.planned_epoch.set(1)

    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert lines == ["[REFUSED] stale authority"]
    assert _options(rest) == []


@pytest.mark.parametrize("state", ["unavailable", None])
async def test_release_never_writes_on_to_an_unreadable_switch(tmp_path, state) -> None:
    rest = FakeRest(switch="Off")
    if state is None:
        del rest.states[_SWITCH]
    else:
        rest.states[_SWITCH] = state
    device = _protect_device(rest, tmp_path)
    hold = _hold(device)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(hold,))

    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert _options(rest) == []
    store = device._execution.authority
    assert store is not None and store.state is not None
    # #449: the record is kept, marked as waiting on a refused release.
    (kept,) = store.state.holds
    assert kept.release_refused_reason == "switch_unreadable"
    assert kept.model_copy(
        update={"release_refused_since": None, "release_refused_reason": None}
    ) == hold


async def test_release_keeps_the_hold_when_the_resident_schedule_is_unreadable(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    rest.states["sensor.solis_control_timed_charge_current"] = "unavailable"
    hold = _hold(device)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(hold,))

    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert _options(rest) == []
    store = device._execution.authority
    assert store is not None and store.state is not None
    # #449: the record is kept, marked as waiting on a refused release.
    (kept,) = store.state.holds
    assert kept.release_refused_reason == "schedule_unverified"
    assert kept.model_copy(
        update={"release_refused_since": None, "release_refused_reason": None}
    ) == hold


async def test_release_leaves_an_unconfirmed_off_alone_and_drops_the_request(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(
        device, ("release_holds", "commit_observe"), holds=(_hold(device, confirmed=False),)
    )

    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert _options(rest) == []
    assert all(is_verified(line) for line in lines)
    store = device._execution.authority
    assert store is not None and store.state is not None and store.state.holds == ()


async def test_release_read_back_mismatch_keeps_the_record(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(_hold(device),))

    async def stuck_off(domain: str, service: str, data: dict[str, object] | None = None):
        rest.calls.append((domain, service, data or {}))
        return []

    rest.call_service = stuck_off  # type: ignore[method-assign]
    with device._execution.handover("release_holds"):
        lines = await device.release_holds()

    assert _options(rest) == ["On"]
    assert [line.outcome for line in lines] == [ActionOutcome.UNVERIFIED]
    store = device._execution.authority
    assert store is not None and store.state is not None
    assert [h.pending_release for h in store.state.holds] == [True]


async def test_release_with_no_records_writes_nothing(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"))

    with device._execution.handover("release_holds"):
        assert await device.release_holds() == []
    assert rest.calls == []


async def test_evaluate_protection_step_may_apply_a_hold(tmp_path) -> None:
    rest = FakeRest(switch="On")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("evaluate_protection",), target="protect")

    with device._execution.handover("evaluate_protection"):
        lines = await device.reconcile_holds(_intent(holds=(_window(-10),)), _now())

    assert _options(rest) == ["Off"]
    assert any(line.outcome is ActionOutcome.VERIFIED for line in lines)


async def test_one_steps_grant_never_carries_to_the_next_action(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    device = _protect_device(rest, tmp_path)
    _in_transition(device, ("release_holds", "commit_observe"), holds=(_hold(device),))
    coordinator = device._execution.coordinator_for(device._config, device._settings)

    async with coordinator.transition():
        with device._execution.handover("commit_observe"):
            first = await device.release_holds()
        with device._execution.handover("release_holds"):
            second = await device.release_holds()

    assert [line.outcome for line in first] == [ActionOutcome.REFUSED]
    assert [line.outcome for line in second] == [ActionOutcome.VERIFIED]


# #429: the Optimise handover operations.

def _optimise_device(rest: FakeRest, tmp_path) -> SolisDevice:
    rest.states["sensor.solis_control_timed_charge_current"] = "40"
    return _device(rest, tmp_path, authority_mode="optimise", solis_fallback_current_a="20")


def _resident_export(rest: FakeRest) -> None:
    """Slot 1 with a charge window and a live export half."""
    for field, value in (
        ("timed_charge_start_hours", "23"), ("timed_charge_start_minutes", "30"),
        ("timed_charge_end_hours", "5"), ("timed_charge_end_minutes", "30"),
        ("timed_discharge_start_hours", "17"), ("timed_discharge_start_minutes", "0"),
        ("timed_discharge_end_hours", "18"), ("timed_discharge_end_minutes", "0"),
    ):
        rest.states[f"sensor.solis_control_{field}"] = value


async def _record_export(device: SolisDevice) -> ExportEventStore:
    store = ExportEventStore(device._settings.db_path)
    async with store:
        await store.save("export|2026-10-21T17:00|2026-10-21T18:00", datetime.now(UTC))
    return store


def _slot_one(rest: FakeRest) -> list[str]:
    fields = (
        "timed_charge_start_hours", "timed_charge_start_minutes",
        "timed_charge_end_hours", "timed_charge_end_minutes",
        "timed_discharge_start_hours", "timed_discharge_start_minutes",
        "timed_discharge_end_hours", "timed_discharge_end_minutes",
    )
    return [rest.states[f"sensor.solis_control_{field}"] for field in fields]


async def test_cancel_export_clears_the_discharge_half_and_the_record(tmp_path) -> None:
    rest = FakeRest(switch="On")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    store = await _record_export(device)
    _in_transition(device, ("cancel_export", "write_fallback", "release_holds", "commit_observe"))

    with device._execution.handover("cancel_export"):
        lines = await device.cancel_export()

    assert all(is_verified(line) for line in lines)
    assert _slot_one(rest) == ["23", "30", "5", "30", "0", "0", "0", "0"]
    assert not store.exists


async def test_cancel_export_failure_keeps_the_record(tmp_path) -> None:
    rest = FakeRest(switch="On")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    store = await _record_export(device)
    _in_transition(device, ("cancel_export", "write_fallback", "release_holds", "commit_observe"))

    async def ignored(domain: str, service: str, data: dict[str, object] | None = None):
        rest.calls.append((domain, service, data or {}))
        return []

    rest.call_service = ignored  # type: ignore[method-assign]
    with device._execution.handover("cancel_export"):
        lines = await device.cancel_export()

    assert not all(is_verified(line) for line in lines)
    assert store.exists


async def test_cancel_export_outside_its_step_writes_nothing(tmp_path) -> None:
    rest = FakeRest(switch="On")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    await _record_export(device)
    _in_transition(device, ("cancel_export", "write_fallback", "release_holds", "commit_observe"))

    with device._execution.handover("release_holds"):
        lines = await device.cancel_export()

    assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert rest.calls == []


async def test_write_fallback_programs_the_fallback_with_no_export(tmp_path) -> None:
    rest = FakeRest(switch="On")
    device = _optimise_device(rest, tmp_path)
    _in_transition(device, ("write_fallback",), target="protect")

    with device._execution.handover("write_fallback"):
        lines = await device.write_fallback()

    assert all(is_verified(line) for line in lines)
    window = (device._settings.charge_window_start, device._settings.charge_window_end)
    start, end = (part.split(":") for part in window)
    assert _slot_one(rest) == [str(int(v)) for v in (*start, *end)] + ["0"] * 4
    assert rest.states["sensor.solis_control_timed_charge_current"] == "20.0"


async def test_write_fallback_refuses_while_an_export_record_remains(tmp_path) -> None:
    rest = FakeRest(switch="On")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    await _record_export(device)
    _in_transition(device, ("cancel_export", "write_fallback"), target="protect")

    with device._execution.handover("write_fallback"):
        lines = await device.write_fallback()

    assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert rest.calls == []


async def test_write_fallback_without_a_valid_schedule_is_blocked(tmp_path) -> None:
    rest = FakeRest(switch="On")
    device = _device(rest, tmp_path, authority_mode="optimise")
    _in_transition(device, ("cancel_export", "write_fallback"), target="protect")

    with device._execution.handover("write_fallback"):
        lines = await device.write_fallback()

    assert [line.outcome for line in lines] == [ActionOutcome.BLOCKED]
    assert rest.calls == []


async def test_optimise_to_observe_handover_in_order(tmp_path) -> None:
    rest = FakeRest(switch="Off")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    store = await _record_export(device)
    _in_transition(
        device, ("cancel_export", "write_fallback", "release_holds", "commit_observe"),
        holds=(_hold(device),),
    )

    authority = device._execution.authority
    assert authority is not None
    for step, run in (
        ("cancel_export", device.cancel_export), ("write_fallback", device.write_fallback),
        ("release_holds", device.release_holds),
    ):
        with device._execution.handover(step):
            assert all(is_verified(line) for line in await run()), step
        # The runner persists each verified step's removal before the next may write.
        assert authority.state is not None and authority.state.transition is not None
        authority.save(authority.state.model_copy(update={"transition": (
            authority.state.transition.model_copy(
                update={"steps": authority.state.transition.steps[1:]}
            )
        )}))

    assert not store.exists
    assert _options(rest) == ["On"]
    assert authority.state is not None
    assert authority.state.holds == ()


async def test_a_later_step_never_writes_before_the_running_one(tmp_path) -> None:
    rest = FakeRest(switch="On")
    _resident_export(rest)
    device = _optimise_device(rest, tmp_path)
    await _record_export(device)
    _in_transition(device, ("cancel_export", "write_fallback", "release_holds", "commit_observe"))

    with device._execution.handover("write_fallback"):
        lines = await device.write_fallback()

    assert [line.outcome for line in lines] == [ActionOutcome.REFUSED]
    assert rest.calls == []
