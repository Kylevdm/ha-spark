"""Native command application never delegates to a legacy hardware writer."""

import pytest

from ha_spark.api.commands import CommandQueue, CommandResult
from ha_spark.api.native_apply import NativeApply, result_lines
from ha_spark.devices.outcomes import ActionOutcome


@pytest.mark.parametrize(("outcome", "expected"), [
    ("applied", ActionOutcome.VERIFIED),
    ("unchanged", ActionOutcome.UNCHANGED_VERIFIED),
    ("deferred", ActionOutcome.BLOCKED),
    ("refused", ActionOutcome.REFUSED),
    ("unverified", ActionOutcome.UNVERIFIED),
    ("failed", ActionOutcome.FAILED),
])
def test_sequence_result_maps_to_every_action(outcome, expected):
    result = CommandResult(session_id="session", sequence=1, outcome=outcome,
                           reason="not_available" if outcome == "refused" else None)
    lines = result_lines(result, 3)
    assert len(lines) == 3
    assert all(line.outcome == expected for line in lines)
    if outcome == "refused":
        assert all("not_available" in line for line in lines)


async def test_timeout_does_not_stack_apply(monkeypatch):
    queue = CommandQueue("session")
    async def no_result(sequence):
        return None
    monkeypatch.setattr(queue, "wait", no_result)
    apply = NativeApply(queue)
    lines = await apply.send([
        {"kind": "export", "inverter_id": "inverter", "request": "none"},
    ], epoch=2)
    assert all(line.outcome == ActionOutcome.FAILED for line in lines)
    assert len(queue.pending) == 1
    lines = await apply.send([], epoch=3)
    assert all("apply pending" in line for line in lines)
    assert len(queue.pending) == 1
    from ha_spark.api.commands import FetchRequest

    queue.fetch(FetchRequest(session_id="session", integration_session_id="receiver",
                             last_result_sequence=0))
    queue.accept(CommandResult(session_id="session", sequence=1, outcome="refused",
                               reason="stale_authority", epoch=3))
    assert not apply.consume_stale_refusal(2)  # no new epoch known yet: no replan
    assert not apply.consume_stale_refusal(None)
    assert apply.consume_stale_refusal(3)
    assert not apply.consume_stale_refusal(3)
    await apply.send([{"kind": "export", "inverter_id": "inverter", "request": "none"}],
                     epoch=3)
    assert queue.pending[2].epoch == 3
    assert len(queue.pending) == 1


def test_no_result_is_failure():
    assert result_lines(None, 1)[0].outcome == ActionOutcome.FAILED


@pytest.mark.parametrize("outcome,reason,accepted", [
    ("applied", None, True), ("unchanged", None, True),
    ("refused", "not_available", False), ("refused", "stale_authority", False),
    ("deferred", None, False), ("failed", None, False), ("unverified", None, False),
])
async def test_run_once_uses_command_not_driver(tmp_path, monkeypatch, outcome, reason, accepted):
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ha_spark.api.commands import FetchRequest
    from ha_spark.authority import AuthorityStore
    from ha_spark.config import Settings
    from ha_spark.devices.execution import ExecutionContext
    from ha_spark.devices.inverters.solis import SolisDevice
    from ha_spark.energy import scheduler
    from ha_spark.energy.models import LoadForecast
    from ha_spark.energy.solar_source import SolarSource
    from tests.native_helpers import NOW, native_link
    from tests.test_scheduler import _plan_at

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    link = native_link(settings)
    context = ExecutionContext()
    context.limits_record = link.topology
    queue = CommandQueue("session")
    apply = NativeApply(queue)
    plan = _plan_at(42.0)
    seen = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    async def current_plan(*args, **kwargs):
        seen.append(kwargs["soc"])
        return SimpleNamespace(
            plan=plan, inputs=SimpleNamespace(ev_hold_charging=None),
            load_source="test", forecast=LoadForecast(total_kwh=24, slots=None, source="test"),
            tz=None,
        )

    async def wait(sequence):
        queue.fetch(FetchRequest(session_id="session", integration_session_id="receiver",
                                 last_result_sequence=0))
        queue.accept(CommandResult(session_id="session", sequence=sequence,
                                   outcome=outcome, reason=reason, epoch=8))
        return queue.results[sequence]

    monkeypatch.setattr(queue, "wait", wait)
    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "current_plan", current_plan)
    for name in ("apply", "reconcile_holds"):
        monkeypatch.setattr(SolisDevice, name, AsyncMock(side_effect=AssertionError("legacy I/O")))
    for name in ("_record_forecast", "_record_solar_forecasts", "_run_derived_rerive"):
        monkeypatch.setattr(scheduler, name, AsyncMock())
    result = await scheduler.run_once(
        settings, execution=context, source=SolarSource("entities", None, None, NOW),
        native_apply=apply,
        native_link=link, native_epoch=7,
    )
    assert seen[0].soc_now == 42.0
    assert (result.applied_plan is plan) is accepted
    assert len(result.apply_lines) == 3
    assert queue.sequence == 1
    assert apply.stale_authority is (reason == "stale_authority")
    assert not queue.pending
    assert all(line.outcome == result_lines(queue.results[1], 1)[0].outcome
               for line in result.apply_lines)


def test_charge_intent_golden():
    import json
    from datetime import time
    from pathlib import Path

    from ha_spark.api.commands import Command
    from ha_spark.api.native_apply import (
        ChargeCurrentLimit,
        ChargeWindow,
        ExportNone,
        build_intents,
    )
    from ha_spark.config import Settings
    from ha_spark.devices.execution import ExecutionContext
    from ha_spark.energy.models import ChargeIntent
    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
    from tests.native_helpers import NOW, native_link

    settings = Settings(battery_capacity_kwh=10, battery_voltage_v=50, charge_efficiency=1)
    link = native_link(settings)
    context = ExecutionContext()
    context.limits_record = link.topology
    intent = ChargeIntent(60, SocMeasurement(SocStatus.OK, NOW, value=0),
                          time(23, 30), time(5, 30))
    intents = build_intents(intent, settings, context, link.topology.topology)
    command = Command(session_id="addon-session", epoch=7, sequence=1, kind="apply",
                      payload={"intents": intents})
    golden = json.loads((Path(__file__).parent / "fixtures/contract/command_apply_intents.json")
                        .read_text())
    assert command.model_dump() == golden
    for model, raw in zip((ChargeWindow, ChargeCurrentLimit, ExportNone), intents, strict=True):
        assert model.model_validate(raw).model_dump() == raw


async def test_retired_gate_and_guard_ignore_snapshot_owner(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from ha_spark.authority import AuthorityStore
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.devices.execution import ExecutionContext, Ownership
    from ha_spark.energy.scheduler import guard_tick
    from ha_spark.ha.rest import HomeAssistantRest
    from tests.native_helpers import native_link

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    context = ExecutionContext()
    context.authority = store
    link = native_link(settings)
    # Even a later, accepted-looking addon snapshot cannot undo the durable retirement.
    link = replace(link, v4=replace(link.v4, owner="addon"))
    assert link.v4.owner == "addon"
    config = DeviceConfig(id="inverter", driver="solis")
    for ownership in Ownership:
        assert context.gate.mode_for(ownership, config, settings).startswith("refuse:")
        assert context.gate.recheck(ownership, config, settings).startswith("refuse:")
    read = AsyncMock(side_effect=AssertionError("guard must not read"))
    write = AsyncMock(side_effect=AssertionError("guard must not write"))
    monkeypatch.setattr(HomeAssistantRest, "get_state", read)
    monkeypatch.setattr(HomeAssistantRest, "call_service", write)
    assert await guard_tick(settings, 2000) == 2000
    read.assert_not_awaited()
    write.assert_not_awaited()


async def test_daemon_replans_stale_authority_once_on_next_tick(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ha_spark.api.commands import FetchRequest
    from ha_spark.authority import AuthorityStore
    from ha_spark.config import Settings
    from ha_spark.devices.execution import execution_context
    from ha_spark.energy import scheduler
    from ha_spark.energy.models import LoadForecast
    from tests.authority_helpers import run_daemon
    from tests.native_helpers import NOW, native_link
    from tests.test_scheduler import _patch_loop, _plan_at

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"),
                        grid_power_entity="sensor.grid", inverter_clock_dst_sync=True)
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    stop = _patch_loop(monkeypatch, [NOW] * 6)
    captured = []
    epochs = []
    plan = _plan_at(42.0)

    def app(state):
        state.commands = CommandQueue("session")
        state.integration_link = native_link(settings)
        captured.append(state)
        return object()

    async def wait(queue, sequence):
        command = queue.pending[sequence]
        epochs.append(command.epoch)
        queue.fetch(FetchRequest(session_id=queue.session_id, integration_session_id="receiver",
                                 last_result_sequence=0))
        result = CommandResult(session_id=queue.session_id, sequence=sequence,
                               outcome="refused" if len(epochs) == 1 else "applied",
                               reason="stale_authority" if len(epochs) == 1 else None, epoch=8)
        queue.accept(result)
        captured[0].integration_link = replace(captured[0].integration_link, authority_epoch=8)
        return result

    async def current_plan(*args, **kwargs):
        assert kwargs["soc"].soc_now == 42.0
        return SimpleNamespace(
            plan=plan, inputs=SimpleNamespace(ev_hold_charging=None, geometry=None),
            load_source="test", forecast=LoadForecast(total_kwh=24, slots=None, source="test"),
            tz=None,
        )

    monkeypatch.setattr(scheduler, "build_app", app)
    monkeypatch.setattr(CommandQueue, "wait", wait)
    monkeypatch.setattr(scheduler, "current_plan", current_plan)
    for name in ("_record_forecast", "_record_solar_forecasts", "_run_derived_rerive",
                 "sample_signals", "record_slot", "readiness_tick"):
        monkeypatch.setattr(scheduler, name, AsyncMock())
    forbidden = {}
    for name in ("soc_monitor_tick", "guard_tick", "reconcile_tick"):
        forbidden[name] = AsyncMock(side_effect=AssertionError("legacy I/O"))
        monkeypatch.setattr(scheduler, name, forbidden[name])
    previous = execution_context().authority
    try:
        with pytest.raises(stop):
            await run_daemon(settings, poll_seconds=0)
    finally:
        execution_context().authority = previous
        execution_context().planned_epoch.set(None)
        execution_context().planned_generation.set(None)
    assert epochs == [7, 8]
    for method in forbidden.values():
        method.assert_not_awaited()


@pytest.mark.parametrize("owner", ["integration", "pending_integration", "pending_addon"])
async def test_snapshot_owner_alone_never_selects_native_path(tmp_path, monkeypatch, owner):
    """Without a durable retirement marker a snapshot's owner changes nothing (0.30.0 path)."""
    from dataclasses import replace

    from ha_spark.config import Settings
    from ha_spark.devices.execution import ExecutionContext
    from ha_spark.energy import scheduler
    from ha_spark.energy.solar_source import SolarSource
    from tests.native_helpers import NOW, native_link

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    link = native_link(settings)
    link = replace(link, v4=replace(link.v4, owner=owner))
    queue = CommandQueue("session")
    sentinel = object()
    seen = []

    async def current_plan(*args, **kwargs):
        seen.append(kwargs["soc"])
        raise RuntimeError("stop after planning input")

    monkeypatch.setattr(scheduler, "current_plan", current_plan)
    with pytest.raises(RuntimeError, match="stop after planning input"):
        await scheduler.run_once(
            settings, soc=sentinel, execution=ExecutionContext(),
            source=SolarSource("entities", None, None, NOW),
            native_apply=NativeApply(queue), native_link=link, native_epoch=7,
        )
    assert seen == [sentinel]  # the caller's (REST-path) measurement, not snapshot SoC
    assert not queue.pending
