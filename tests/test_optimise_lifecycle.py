"""Owner requests activate Optimise and hand charging back in verified order."""
import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.authority import AuthorityStore, FallbackState
from ha_spark.daemon_channel import submit_socket
from ha_spark.devices.execution import execution_context
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.energy.scheduler import soc_monitor_tick
from ha_spark.energy.soc_monitor import SocMonitor
from ha_spark.transitions import PreconditionReport
from tests.test_daemon_executor import running_daemon, settings_at
from tests.test_mode_lifecycle import _hold


async def wait_for(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.001)
    assert predicate()


@pytest.mark.parametrize("entry_mode", ["observe", "protect"])
@pytest.mark.parametrize("outcome", [ActionOutcome.FAILED, ActionOutcome.UNVERIFIED,
                                     ActionOutcome.SIMULATED])
@respx.mock
async def test_optimise_activation_retries_without_preview_baseline(
    tmp_path, monkeypatch, caplog, outcome, entry_mode,
):
    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": entry_mode}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport()))
    prior = execution_context().authority
    caplog.set_level("INFO")
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as rig:
            state, calls, device, measurement = rig
            # This observation ends the rig's initial monitor fallback so a
            # fresh entry measurement permits normal planning.
            async def observe(settings, monitor):
                monitor.fallback_active = False
                monitor.recovery_blocked = False
                return measurement

            monkeypatch.setattr("ha_spark.energy.scheduler.soc_monitor_tick", observe)
            device.apply.return_value = [ActionLine("preview", ActionOutcome.SIMULATED)]
            preview = await submit_socket(settings, "apply")
            assert preview.error is None
            assert device.apply.await_count > 0
            # The preview is accepted as a baseline in Observe/Protect only.
            device.apply.return_value = [ActionLine("not verified", outcome)]
            current = state.executor.authority.state
            reply = await state.executor.submit_mode("optimise", current.epoch)
            assert reply.error is None
            assert reply.authority["epoch"] == current.epoch + 1
            await wait_for(lambda: state.executor.authority.state.transition is not None
                           and state.executor.authority.state.transition.failing_actions)
            current = state.executor.authority.state
            assert current.committed_mode == "optimise"
            assert current.transition.steps == ("verify_fresh_plan",)
            assert "not verified" in current.transition.failing_actions
            assert calls[-1]["previous_plan"] is None
            attempts = device.apply.await_count
            protection_passes = device.reconcile_holds.await_count
            await wait_for(lambda: device.apply.await_count > attempts)
            assert device.reconcile_holds.await_count > protection_passes
            assert state.executor.authority.state.transition is not None
            device.apply.return_value = [ActionLine("verified", ActionOutcome.VERIFIED)]
            await wait_for(lambda: state.executor.authority.state.transition is None)
            assert caplog.text.count("disable any pre-existing automations") == 1
    finally:
        execution_context().authority = prior


@pytest.mark.parametrize("target,remapped", [("protect", False), ("observe", False),
                                             ("observe", True)])
@respx.mock
async def test_optimise_handover_orders_steps_and_clears_fallback(
    tmp_path, monkeypatch, target, remapped,
):
    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    hold = _hold(settings)
    if remapped:
        hold = hold.model_copy(update={"device": hold.device.model_copy(update={
            "power_switch": "switch.old_inverter",
        })})
    store.save(store.state.model_copy(update={
        "committed_mode": "optimise", "holds": (hold,),
        "fallback": FallbackState(active=True, causes=("soc_integrity",)),
    }))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            order = []

            async def cancel():
                if remapped:
                    assert not state.executor.authority.state.holds
                order.append("cancel")
                return [ActionLine("cancel verified", ActionOutcome.VERIFIED)]

            async def fallback():
                order.append("fallback")
                return [ActionLine("fallback failed", ActionOutcome.UNVERIFIED)]

            async def release():
                order.append("release")
                state.executor.authority.save_holds(())
                return [ActionLine("release verified", ActionOutcome.VERIFIED)]

            device.cancel_export.side_effect = cancel
            device.write_fallback.side_effect = fallback
            device.release_holds.side_effect = release
            epoch = state.executor.authority.state.epoch
            reply = await state.executor.submit_mode(target, epoch)
            assert reply.error is None
            await wait_for(lambda: state.executor.authority.state.transition
                           and state.executor.authority.state.transition.failing_actions)
            current = state.executor.authority.state
            assert current.committed_mode == "optimise"
            assert current.transition.steps[0] == "write_fallback"
            assert order[:2] == ["cancel", "fallback"]
            assert "release" not in order
            assert current.fallback.active
            device.write_fallback.side_effect = None
            device.write_fallback.return_value = [ActionLine("fallback verified",
                                                           ActionOutcome.VERIFIED)]
            await wait_for(lambda: state.executor.authority.state.transition is None)
            current = state.executor.authority.state
            assert current.committed_mode == target
            assert current.epoch == epoch + 2
            assert not current.fallback.active
            assert current.fallback.causes == ()
            if target == "protect":
                assert len(current.holds) == 1
                assert current.holds[0].device == hold.device
                assert current.holds[0].confirmed
            elif not remapped:
                assert order[-1] == "release"
            device.cancel_export.assert_awaited_once()
    finally:
        execution_context().authority = prior


@pytest.mark.parametrize("entry_mode", ["observe", "protect"])
@respx.mock
async def test_optimise_entry_checks_this_ticks_soc_and_reports_all_preconditions(
    tmp_path, monkeypatch, entry_mode,
):
    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": entry_mode}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    checker = AsyncMock(return_value=PreconditionReport(
        protect=("no validated hold",), optimise=("untrusted SoC", "invalid fallback"),
    ))
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions", checker)
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as rig:
            state, _, device, measurement = rig
            epoch = state.executor.authority.state.epoch
            reply = await state.executor.submit_mode("optimise", epoch)
            assert reply.error == "preconditions_unmet"
            assert reply.text == "no validated hold; untrusted SoC; invalid fallback"
            assert checker.await_args.args[2] is measurement
            current = state.executor.authority.state
            assert current.epoch == epoch
            assert current.committed_mode == entry_mode
            assert current.transition is None
            device.apply.assert_not_awaited()
    finally:
        execution_context().authority = prior


@pytest.mark.parametrize("mode", ["protect", "observe"])
@respx.mock
async def test_monitor_fallback_is_not_reclaimed_outside_optimise(tmp_path, mode):
    settings = settings_at(tmp_path, soc_entity="sensor.soc", solis_fallback_current_a=20)
    prior = execution_context().authority
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": mode}))
    execution_context().authority = store
    # The monitor's failure/recovery history survives a mode handover. It is
    # still the judge of the cause, but cannot claim authority in another mode.
    (tmp_path / "ha_spark_soc_monitor.json").write_text(
        '{"consecutive_failures": 3, "fallback_status": "fallback_verified"}',
    )
    monitor = SocMonitor.load(settings)
    assert monitor.fallback_active
    respx.get("http://ha.test/api/states/sensor.soc").respond(200, json={
        "entity_id": "sensor.soc", "state": "50", "attributes": {},
        "last_reported": datetime.now(UTC).isoformat(),
    })
    posts = respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    try:
        epoch = store.state.epoch
        for _ in range(2):
            assert await soc_monitor_tick(settings, monitor) is not None
        assert monitor.fallback_active
        assert not store.state.fallback.active
        assert store.state.epoch == epoch
        assert all("/states/" in call.request.url.path for call in posts.calls)
    finally:
        execution_context().authority = prior


@respx.mock
async def test_activation_completes_past_unverified_protection_notes_and_guard_may_act(
    tmp_path, monkeypatch,
):
    from ha_spark.devices.execution import Ownership

    settings = settings_at(
        tmp_path, grid_power_entity="sensor.grid",
        charge_window_start="00:00", charge_window_end="23:59",
    )
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": "protect"}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport()))
    monkeypatch.setattr("ha_spark.energy.scheduler._charger_supports_live_rate",
                        AsyncMock(return_value=True))
    guard_grants: list[str] = []

    async def guard(settings, target_w, **kwargs):
        context = execution_context()
        config = next(d for d in settings.devices if d.type == "inverter")
        authority = context.authority
        if authority is not None and authority.state.transition is not None:
            guard_grants.append(context.gate.mode_for(Ownership.CHARGING, config, settings))
        return target_w

    monkeypatch.setattr("ha_spark.energy.scheduler.guard_tick", guard)
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as rig:
            state, _, device, measurement = rig

            async def observe(settings, monitor):
                monitor.fallback_active = False
                monitor.recovery_blocked = False
                return measurement

            monkeypatch.setattr("ha_spark.energy.scheduler.soc_monitor_tick", observe)
            # An owner-Off switch is left alone: a note, not a failed action.
            device.reconcile_holds.return_value = [
                ActionLine("[SKIP] owner-Off switch left as-is", ActionOutcome.UNVERIFIED)
            ]
            failing = [ActionLine("not verified", ActionOutcome.UNVERIFIED)]
            device.apply.return_value = failing
            current = state.executor.authority.state
            reply = await state.executor.submit_mode("optimise", current.epoch)
            assert reply.error is None
            await wait_for(lambda: guard_grants)
            assert state.executor.authority.state.transition is not None
            assert guard_grants[0] == "on"
            device.apply.return_value = [ActionLine("verified", ActionOutcome.VERIFIED)]
            await wait_for(lambda: state.executor.authority.state.transition is None)
    finally:
        execution_context().authority = prior
