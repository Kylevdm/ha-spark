"""Shared bus actions keep today's permission and hardware sequencing intact."""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from ha_spark.devices import get_device, inverter_device
from ha_spark.devices.base import ControlAuthority
from ha_spark.devices.execution import ExecutionContext, Ownership
from ha_spark.devices.inverters.alphaess import AlphaESSDevice
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.energy.supply_guard import SupplyGuard
from tests.authority_helpers import legacy_settings as Settings
from tests.authority_helpers import seeded_execution
from tests.test_solis_power_switch import FakeRest, _intent, _now


@pytest.mark.parametrize("control", list(ControlAuthority))
@pytest.mark.parametrize("mode", ["off", "simulate", "on"])
@pytest.mark.parametrize("ownership", list(Ownership))
def test_legacy_keys_grant_nothing_to_an_unbound_context(control, mode, ownership):
    settings = Settings(proactive_mode=mode)
    config = settings.devices[0].model_copy(update={"control": control})
    context = ExecutionContext()
    assert context.gate.mode_for(ownership, config, settings) == "refuse:authority unknown"


def test_all_construction_paths_share_the_bus():
    settings = Settings()
    rest = FakeRest()
    scheduler = inverter_device(settings, rest)
    cli = get_device(settings.devices[0], settings, rest)
    guard = SupplyGuard(settings, rest)
    direct = SolisDevice(settings.devices[0], settings, rest)
    assert scheduler._coordinator is cli._coordinator is guard._coordinator is direct._coordinator


# Each former effective_mode site has an explicit ownership declaration. Helpers
# are also callable directly; their declarations must not rely on a caller.
@pytest.mark.parametrize("driver,site,owner", [
    (SolisDevice, "apply_fallback", Ownership.CHARGING),
    (SolisDevice, "_apply", Ownership.CHARGING),
    (SolisDevice, "_write_charge_window", Ownership.CHARGING),
    (SolisDevice, "_clear_discharge_window", Ownership.CHARGING),
    (SolisDevice, "_write_charge_current_result", Ownership.CHARGING),
    (SolisDevice, "_prepare_slot_one_current", Ownership.CHARGING),
    (SolisDevice, "_zero_slot", Ownership.CHARGING),
    (SolisDevice, "_set_current_result", Ownership.CHARGING),
    (SolisDevice, "_reconcile_power_switch", Ownership.PROTECTION),
    (SolisDevice, "_write_safe_power_switch", Ownership.PROTECTION),
    (SolisDevice, "_write_safe_slot_block", Ownership.CHARGING),
    (SolisDevice, "_write_discharge_current_result", Ownership.CHARGING),
    (SolisDevice, "sync_clock", Ownership.CLOCK),
    (AlphaESSDevice, "apply", Ownership.CHARGING),
    (SupplyGuard, "tick", Ownership.CHARGING),
])
def test_write_site_ownership(driver, site, owner):
    assert getattr(driver, site).ownership is owner


@pytest.mark.parametrize("pause_at", ["deactivated", "current_confirmed"])
async def test_apply_is_indivisible_across_driver_instances(tmp_path, monkeypatch, pause_at):
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    settings = Settings(proactive_mode="on", db_path=str(tmp_path / "events.db"))
    rest = FakeRest()
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "1"
    rest.states["sensor.solis_control_timed_charge_current"] = "5"
    context = seeded_execution(settings)
    context.gate = Mock(wraps=context.gate)
    first = SolisDevice(settings.devices[0], settings, rest, execution=context)
    second = SolisDevice(settings.devices[0], settings, rest, execution=context)
    paused, resume, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    trace = []
    zero_slot = first._zero_slot
    set_current = first._set_current_result
    verify_slot = first._verify_slot_block
    second_apply = second._apply_current

    async def pause():
        paused.set()
        await resume.wait()

    async def zero(*args, **kwargs):
        result = await zero_slot(*args, **kwargs)
        if args[0] == 1 and pause_at == "deactivated":
            await pause()
        return result

    async def current(*args, **kwargs):
        result = await set_current(*args, **kwargs)
        if pause_at == "current_confirmed":
            await pause()
        return result

    async def verify(*args, **kwargs):
        result = await verify_slot(*args, **kwargs)
        trace.append("first read-back")
        return result

    async def other(*args, **kwargs):
        second_started.set()
        trace.append("second started")
        return await second_apply(*args, **kwargs)

    monkeypatch.setattr(first, "_zero_slot", zero)
    monkeypatch.setattr(first, "_set_current_result", current)
    monkeypatch.setattr(first, "_verify_slot_block", verify)
    monkeypatch.setattr(second, "_apply_current", other)
    task = asyncio.create_task(first.apply(_intent()))
    await asyncio.wait_for(paused.wait(), 2)
    waiting = asyncio.create_task(second.set_charge_rate(1000))
    await asyncio.sleep(0)
    assert not second_started.is_set()
    assert not waiting.done()
    resume.set()
    await asyncio.wait_for(asyncio.gather(task, waiting), 2)
    assert trace[-1] == "second started"
    assert context.gate.mode_for.call_count == 2  # one per complete action, not per helper


async def test_exception_and_cancellation_release_the_bus():
    settings = Settings()
    config = settings.devices[0]
    context = ExecutionContext()
    bus = context.coordinator_for(config, settings)
    with pytest.raises(RuntimeError):
        async with bus.action(context.gate, Ownership.CHARGING, config, settings):
            async with bus.action(context.gate, Ownership.CHARGING, config, settings):
                raise RuntimeError("action failed")
    entered = asyncio.Event()

    async def cancelled():
        async with bus.action(context.gate, Ownership.CHARGING, config, settings):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1):
        async with bus.action(context.gate, Ownership.CLOCK, config, settings):
            pass


async def test_production_actions_ask_one_gate_with_their_owner(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    settings = Settings(proactive_mode="simulate", db_path=str(tmp_path / "events.db"),
                        solis_fallback_current_a="20")
    context = seeded_execution(settings)
    context.gate = Mock(wraps=context.gate)
    device = SolisDevice(settings.devices[0], settings, FakeRest(), execution=context)
    for action, owners in [
        (lambda: device.apply(_intent()), [Ownership.CHARGING]),
        (lambda: device.apply_fallback(_intent().soc), [Ownership.CHARGING]),
        (lambda: device.reconcile_holds(_intent(), _now()), [Ownership.PROTECTION]),
        (device.write_safe_state, [Ownership.PROTECTION, Ownership.CHARGING]),
        (device.sync_clock, [Ownership.CLOCK]),
        (lambda: device.set_charge_rate(1000), [Ownership.CHARGING]),
    ]:
        context.gate.reset_mock()
        await action()
        assert [call.args[0] for call in context.gate.mode_for.call_args_list] == owners


async def test_supply_guard_holds_bus_from_live_read_through_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    settings = Settings(proactive_mode="on", db_path=str(tmp_path / "events.db"),
                        grid_power_entity="sensor.grid")
    rest = FakeRest()
    rest.states["sensor.grid"] = "15000"
    rest.states["sensor.solis_control_timed_charge_current"] = "20"
    context = seeded_execution(settings)
    context.gate = Mock(wraps=context.gate)
    guard = SupplyGuard(settings, rest, execution=context)
    other = inverter_device(settings, rest, execution=context)
    paused, resume, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    read = guard._charger.read_charge_rate
    verify = guard._charger._verify_current
    apply = other._apply_current
    trace = []

    async def live_read():
        value = await read()
        paused.set()
        await resume.wait()
        return value

    async def read_back(*args, **kwargs):
        result = await verify(*args, **kwargs)
        trace.append("guard read-back")
        return result

    async def other_action(*args, **kwargs):
        started.set()
        trace.append("other action")
        return await apply(*args, **kwargs)

    monkeypatch.setattr(guard._charger, "read_charge_rate", live_read)
    monkeypatch.setattr(guard._charger, "_verify_current", read_back)
    monkeypatch.setattr(other, "_apply_current", other_action)
    task = asyncio.create_task(guard.tick(0))
    await asyncio.wait_for(paused.wait(), 2)
    waiting = asyncio.create_task(other.set_charge_rate(1000))
    await asyncio.sleep(0)
    assert not started.is_set()
    resume.set()
    await asyncio.wait_for(asyncio.gather(task, waiting), 2)
    assert trace == ["guard read-back", "other action"]
    assert context.gate.mode_for.call_count == 2


async def test_failed_device_action_does_not_stop_the_next(tmp_path, monkeypatch):
    from ha_spark.devices.outcomes import ActionOutcome

    settings = Settings(proactive_mode="on", db_path=str(tmp_path / "events.db"))
    context = seeded_execution(settings)
    first = SolisDevice(settings.devices[0], settings, FakeRest(), execution=context)
    second = SolisDevice(settings.devices[0], settings, FakeRest(), execution=context)
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)

    async def fail(*args, **kwargs):
        raise RuntimeError("device unavailable")

    monkeypatch.setattr(first, "_apply_current", fail)
    assert (await first.set_charge_rate(1000)).outcome is ActionOutcome.FAILED
    async with asyncio.timeout(1):
        assert (await second.set_charge_rate(1000)).outcome is ActionOutcome.VERIFIED


async def test_dst_entry_point_threads_the_context(monkeypatch):
    from ha_spark.energy.dst_clock_sync import DstClockSync

    settings = Settings(proactive_mode="simulate")
    context = seeded_execution(settings)
    context.gate = Mock(wraps=context.gate)
    rest = FakeRest()
    constructed = []

    def factory(*args, **kwargs):
        device = inverter_device(*args, **kwargs)
        constructed.append(device)
        return device

    sync = DstClockSync()
    monkeypatch.setattr(sync, "due", lambda now: now)
    monkeypatch.setattr("ha_spark.energy.dst_clock_sync.inverter_device", factory)
    await sync.run(settings, rest, _now(), execution=context)
    assert constructed[0]._coordinator is context.coordinator_for(settings.devices[0], settings)
    assert context.gate.mode_for.call_args.args[0] is Ownership.CLOCK
    assert context.gate.mode_for.call_count == 1


async def test_alphaess_uses_the_charging_gate():
    settings = Settings(proactive_mode="simulate")
    config = settings.devices[0].model_copy(update={"driver": "alphaess"})
    context = seeded_execution(settings)
    context.gate = Mock(wraps=context.gate)
    device = AlphaESSDevice(config, settings, FakeRest(), execution=context)
    await device.apply(_intent())
    context.gate.mode_for.assert_called_once_with(Ownership.CHARGING, config, settings)
