"""Public daemon requests drive durable owner handovers."""
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx

from ha_spark import cli
from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord, Transition
from ha_spark.daemon_channel import submit_socket
from ha_spark.devices.execution import execution_context
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.transitions import PreconditionReport
from tests.test_daemon_executor import running_daemon, settings_at


def _hold(settings) -> HoldRecord:
    """A confirmed hold on the configured inverter, so → Observe has one to release."""
    config = settings.devices[0]
    return HoldRecord(cause="ev", started_at=datetime.now(UTC), confirmed=True, device=HoldDevice(
        driver=config.driver, power_switch=config.entities.get("power_switch", ""),
        hub=settings.solis_control_hub, slave=settings.solis_modbus_slave,
    ))


@pytest.mark.parametrize("target,error", [("optimise", "preconditions_unmet"),
                                         ("protect", "stale_epoch")])
@respx.mock
async def test_daemon_refuses_unavailable_or_stale_mode(tmp_path, monkeypatch, target, error):
    settings = settings_at(tmp_path)
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch):
            reply = await submit_socket(settings, "mode", target=target, expected_epoch=999)
            assert reply.error == error
    finally:
        execution_context().authority = prior


@respx.mock
async def test_daemon_protect_and_observe_handover(tmp_path: Path, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": "observe"}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport()))
    prior = execution_context().authority
    caplog.set_level("INFO")
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            current = state.executor.authority.state
            reply = await submit_socket(
                settings, "mode", target="protect", expected_epoch=current.epoch,
            )
            assert reply.error is None
            assert reply.authority["mode"] == "protect"
            for _ in range(100):
                if state.executor.authority.state.transition is None:
                    break
                await asyncio.sleep(0.001)
            assert state.executor.authority.state.transition is None
            device.release_holds.return_value = [ActionLine("verified", ActionOutcome.VERIFIED)]
            state.executor.authority.save_holds((_hold(settings),))
            current = state.executor.authority.state
            reply = await state.executor.submit_mode("observe", current.epoch, ingress_user="owner")
            assert reply.error is None
            for _ in range(100):
                if state.executor.authority.state.committed_mode == "observe":
                    break
                await asyncio.sleep(0.001)
            assert state.executor.authority.state.committed_mode == "observe"
            device.release_holds.assert_awaited_once()
            audit = [r.message for r in caplog.records if "Operating mode requester=" in r.message]
            assert len(audit) == 2
            assert "requester=cli old=observe target=protect" in audit[0]
            assert "requester=owner old=protect target=observe" in audit[1]
            assert "SECRET" not in " ".join(audit)
    finally:
        execution_context().authority = prior


@pytest.mark.parametrize("outcome", [ActionOutcome.FAILED, ActionOutcome.UNVERIFIED,
                                     ActionOutcome.SIMULATED])
@respx.mock
async def test_failed_release_stays_switching_and_repeated_observe_joins(
    tmp_path, monkeypatch, outcome,
):
    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={
        "committed_mode": "protect", "holds": (_hold(settings),),
    }))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            device.release_holds.return_value = [ActionLine("release not verified", outcome)]
            current = state.executor.authority.state
            reply = await state.executor.submit_mode("observe", current.epoch)
            epoch = reply.authority["epoch"]
            for _ in range(100):
                current = state.executor.authority.state
                if current.transition and current.transition.failing_actions:
                    break
                await asyncio.sleep(0.001)
            assert current.committed_mode == "protect"
            assert current.transition.failing_actions == ("release not verified",)
            blocked = await state.executor.submit_mode("protect", epoch)
            assert blocked.error == "transition_in_progress"
            joined = await state.executor.submit_mode("observe", epoch)
            assert joined.error is None
            assert joined.authority["epoch"] == epoch
            async def released():
                state.executor.authority.save_holds(())
                return [ActionLine("released", ActionOutcome.VERIFIED)]

            device.release_holds.side_effect = released
            for _ in range(100):
                if state.executor.authority.state.transition is None:
                    break
                await asyncio.sleep(0.001)
            assert state.executor.authority.state.committed_mode == "observe"
    finally:
        execution_context().authority = prior


@respx.mock
async def test_cli_confirm_unknown_observe_has_no_device_writes(tmp_path, monkeypatch, capsys):
    settings = settings_at(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    services = respx.post(url__regex=r"http://ha.test/api/services/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            store = state.executor.authority
            original_id = store.state.store_id
            store.store_path.unlink()
            assert await asyncio.to_thread(cli.main, ["mode", "confirm-observe"]) == 0
            assert store.known
            assert store.state.store_id != original_id
            assert store.state.committed_mode == "observe"
            assert store.state.epoch == 1
            assert all("/persistent_notification/" in call.request.url.path
                       for call in services.calls)
            device.release_holds.assert_not_awaited()
            assert await asyncio.to_thread(cli.main, ["mode", "confirm-observe"]) == 1
            assert "mode set observe" in capsys.readouterr().out
    finally:
        execution_context().authority = prior


@respx.mock
async def test_failed_authority_save_never_grants_protect(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": "observe"}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport()))
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            store = state.executor.authority
            epoch = store.state.epoch

            def fail(*args):
                raise OSError("not logged")

            monkeypatch.setattr("ha_spark.authority._durable_write", fail)
            reply = await state.executor.submit_mode("protect", epoch)
            assert reply.error == "authority persistence failed"
            assert store.state.committed_mode == "observe"
            assert store.state.epoch == epoch
            assert store.save_failed
            assert store.state.transition is None
            device.release_holds.assert_not_awaited()
    finally:
        execution_context().authority = prior


@respx.mock
async def test_transition_waits_for_inflight_bus_action(tmp_path, monkeypatch):
    from ha_spark.devices.execution import Ownership

    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": "protect"}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    context = execution_context()
    prior = context.authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            device.release_holds.return_value = []
            epoch = state.executor.authority.state.epoch
            bus = context.coordinator_for(settings.devices[0], settings)
            async with bus.action(
                context.gate, Ownership.PROTECTION, settings.devices[0], settings,
            ):
                request = asyncio.create_task(state.executor.submit_mode("observe", epoch))
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not request.done()
                assert state.executor.authority.state.epoch == epoch
                device.release_holds.assert_not_awaited()
            reply = await asyncio.wait_for(request, 2)
            assert reply.error is None
            assert reply.authority["epoch"] == epoch + 1
    finally:
        context.authority = prior


@respx.mock
async def test_restart_resumes_stored_observe_handover(tmp_path, monkeypatch):


    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={
        "committed_mode": "protect", "holds": (_hold(settings),),
        "transition": Transition(target="observe", started_at=datetime.now(UTC),
                                 steps=("release_holds", "commit_observe")),
    }))
    epoch = store.state.epoch
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            async def released():
                state.executor.authority.save_holds(())
                return [ActionLine("released", ActionOutcome.VERIFIED)]

            device.release_holds.side_effect = released
            for _ in range(100):
                if state.executor.authority.state.transition is None:
                    break
                await asyncio.sleep(0.001)
            result = state.executor.authority.state
            assert result.committed_mode == "observe"
            assert result.transition is None
            assert result.epoch >= epoch + 2
            device.release_holds.assert_awaited_once()
    finally:
        execution_context().authority = prior


@pytest.mark.parametrize("confirmed", [True, False])
@respx.mock
async def test_protect_completes_once_evaluated_and_any_hold_verified(
    tmp_path, monkeypatch, confirmed,
):
    from unittest.mock import AsyncMock

    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.save(store.state.model_copy(update={"committed_mode": "observe"}))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport()))
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            async def evaluate(intent, now):
                # An owner-Off switch is left alone (unverified, not a failure); a hold
                # request that never read back is not a verified hold.
                if not confirmed:
                    state.executor.authority.save_holds(
                        (_hold(settings).model_copy(update={"confirmed": False}),)
                    )
                return [ActionLine("[SKIP] owner-Off switch left as-is", ActionOutcome.UNVERIFIED)]

            device.reconcile_holds.side_effect = evaluate
            current = state.executor.authority.state
            reply = await submit_socket(
                settings, "mode", target="protect", expected_epoch=current.epoch,
            )
            assert reply.error is None
            for _ in range(100):
                current = state.executor.authority.state
                if current.transition is None or current.transition.failing_actions:
                    break
                await asyncio.sleep(0.001)
            assert (current.transition is None) is confirmed
    finally:
        execution_context().authority = prior
