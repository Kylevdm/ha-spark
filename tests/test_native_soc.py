"""Native planning reads only the accepted snapshot, never a REST SoC substitute."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.api.native_apply import snapshot_soc
from ha_spark.api.snapshot_v4 import BankSoc
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.solar_source import SolarSource
from ha_spark.ha.rest import HomeAssistantRest
from tests.native_helpers import NOW, native_link


@pytest.mark.parametrize("condition,ok", [
    ("trusted", True), ("untrusted", False), ("recovering", False),
    ("missing", False), ("stale_snapshot", False), ("stale_read", False),
    ("future_read", False), ("future_read_1s", False), ("skew", False),
])
def test_snapshot_soc(condition, ok):
    settings = Settings(ha_url="http://ha.test", ha_token="test")
    link = native_link(settings)
    if condition == "stale_snapshot":
        link = replace(link, received_at=NOW - timedelta(seconds=91))
    elif condition == "skew":
        link = replace(link, skew=True)
    else:
        bank = link.v4.banks[0]
        soc = bank.soc
        if condition in ("untrusted", "recovering"):
            soc = soc.model_copy(update={"trust": condition})
        elif condition == "missing":
            soc = None
        elif condition == "stale_read":
            soc = soc.model_copy(update={"read_at": NOW - timedelta(days=1)})
        elif condition == "future_read":
            soc = soc.model_copy(update={"read_at": NOW + timedelta(seconds=6)})
        elif condition == "future_read_1s":
            soc = soc.model_copy(update={"read_at": NOW + timedelta(seconds=1)})
        link = replace(link, v4=replace(link.v4, banks=(BankSoc(id=bank.id, soc=soc),)))
    measurement = snapshot_soc(link, NOW, settings)
    assert measurement.ok is ok
    assert measurement.soc_now == (42.0 if ok else 0.0)


@respx.mock
async def test_retired_preview_never_reads_rest_soc(tmp_path, monkeypatch):
    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    observe = AsyncMock(side_effect=AssertionError("REST SoC must not be read"))
    monkeypatch.setattr("ha_spark.energy.sources.observe_soc", observe)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest, source=SolarSource("entities", None, None, NOW))
    assert not run.plan.soc.ok
    assert not run.plan.charge_intent.soc.ok
    observe.assert_not_awaited()


@respx.mock
@pytest.mark.parametrize("condition", ["untrusted", "recovering", "missing", "stale"])
async def test_untrusted_native_soc_never_enqueues_charge(tmp_path, monkeypatch, condition):
    from datetime import datetime

    from ha_spark.api.commands import CommandQueue
    from ha_spark.api.native_apply import NativeApply
    from ha_spark.devices.execution import ExecutionContext
    from ha_spark.energy.scheduler import run_once

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    link = native_link(settings)
    bank = link.v4.banks[0]
    soc = bank.soc
    if condition in ("untrusted", "recovering"):
        soc = soc.model_copy(update={"trust": condition})
    elif condition == "missing":
        soc = None
    else:
        link = replace(link, received_at=NOW - timedelta(seconds=91))
    link = replace(link, v4=replace(link.v4, banks=(BankSoc(id=bank.id, soc=soc),)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("ha_spark.energy.scheduler.datetime", Clock)
    observe = AsyncMock(side_effect=AssertionError("REST SoC must not be read"))
    monkeypatch.setattr("ha_spark.energy.sources.observe_soc", observe)
    context = ExecutionContext()
    context.limits_record = link.topology
    queue = CommandQueue("session")
    result = await run_once(
        settings, execution=context, source=SolarSource("entities", None, None, NOW),
        native_apply=NativeApply(queue), native_link=link, native_epoch=7,
    )
    assert not result.plan.soc.ok
    assert result.applied_plan is None
    assert not queue.pending
    assert queue.sequence == 0
    observe.assert_not_awaited()


@respx.mock
async def test_retired_gather_inputs_never_reads_rest_soc(tmp_path, monkeypatch):
    """Every gather_inputs caller (agent get_state included) is covered, not just current_plan."""
    from ha_spark.energy.sources import gather_inputs

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    observe = AsyncMock(side_effect=AssertionError("REST SoC must not be read"))
    monkeypatch.setattr("ha_spark.energy.sources.observe_soc", observe)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _cfg, _forecast = await gather_inputs(
            settings, rest, source=SolarSource("entities", None, None, NOW),
        )
    assert not inputs.soc.ok
    observe.assert_not_awaited()


async def test_pending_apply_reported_even_when_soc_blocks(tmp_path, monkeypatch):
    from datetime import datetime

    from ha_spark.api.commands import CommandQueue
    from ha_spark.api.native_apply import NativeApply
    from ha_spark.devices.execution import ExecutionContext
    from ha_spark.devices.outcomes import ActionOutcome
    from ha_spark.energy.scheduler import run_once

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    link = native_link(settings)
    link = replace(link, v4=replace(link.v4, banks=(BankSoc(id=link.v4.banks[0].id),)))
    queue = CommandQueue("session")
    queue.enqueue(epoch=7, kind="apply", payload={"intents": []})
    apply = NativeApply(queue)
    apply.sequence, apply.epoch = 1, 7

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("ha_spark.energy.scheduler.datetime", Clock)
    monkeypatch.setattr("ha_spark.energy.sources.observe_soc",
                        AsyncMock(side_effect=AssertionError("REST SoC must not be read")))
    context = ExecutionContext()
    context.limits_record = link.topology
    with respx.mock:
        result = await run_once(
            settings, execution=context, source=SolarSource("entities", None, None, NOW),
            native_apply=apply, native_link=link, native_epoch=7,
        )
    assert [str(line) for line in result.apply_lines] == ["[SKIP] apply pending"]
    assert result.apply_lines[0].outcome is ActionOutcome.BLOCKED
    assert result.applied_plan is None
    assert list(queue.pending) == [1]
