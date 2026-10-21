"""The daemon closes readiness days after capturing the new boundary slot."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.energy import scheduler
from ha_spark.energy.readiness_measurements import ReadinessDays
from tests.authority_helpers import seed_authority
from tests.test_authority_gate import daemon_ticks as control_ticks
from tests.test_readiness_ledger import START, revision
from tests.test_readiness_measurements import measured, seed  # noqa: F401


def daemon_ticks(monkeypatch, ticks):
    """Allow background day-close evidence to finish before this test shuts down."""
    done = asyncio.Event()
    record = scheduler.record_closed_days

    async def complete(*args, **kwargs):
        try:
            await record(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(scheduler, "record_closed_days", complete)
    stop = control_ticks(monkeypatch, ticks)
    advance = asyncio.sleep

    async def sleep(seconds):
        if seconds == 123:
            await asyncio.wait_for(done.wait(), timeout=2)
        await advance(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return stop


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch):
    """These tests pin cadence and retry, not the settle delay (tested on its own)."""
    monkeypatch.setattr("ha_spark.energy.readiness_measurements.SETTLE_DELAY", timedelta(0))



@respx.mock
async def test_daemon_snapshots_previous_day_and_writes_no_hardware(
    measured, monkeypatch,  # noqa: F811 - shared fixture
):
    settings, _ = measured
    settings.ha_url = "http://ha.test"
    settings.ha_token = "t"
    seed_authority(settings, "observe")
    await seed(settings)
    a = revision()
    monkeypatch.setattr(scheduler, "current_plan", AsyncMock(return_value=a.run))
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    stop = daemon_ticks(monkeypatch, [START + timedelta(days=1, hours=1)])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    (day,) = await ReadinessDays(settings.db_path).days()
    assert day.local_date == START.date()
    assert day.qualifying, day.reasons
    assert not any("/api/services/" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_daemon_tick_survives_a_raising_day_close_ledger(
    measured, monkeypatch,  # noqa: F811 - shared fixture
):
    settings, _ = measured
    settings.ha_url = "http://ha.test"
    settings.ha_token = "t"
    seed_authority(settings, "observe")
    await seed(settings)
    a = revision()
    monkeypatch.setattr(scheduler, "current_plan", AsyncMock(return_value=a.run))
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())

    class RaisingLedger:
        def __init__(self, db_path):
            self.db_path = db_path

        async def first_slot_start(self):
            raise RuntimeError("ledger unreadable")

        async def slots(self, start, end):
            raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(
        "ha_spark.energy.readiness_measurements.ReadinessLedger", RaisingLedger
    )
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    stop = daemon_ticks(monkeypatch, [START + timedelta(days=1, hours=1)])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert await ReadinessDays(settings.db_path).days() == []
    assert not any("/api/services/" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_day_close_join_never_delays_the_guard_tick(
    measured, monkeypatch,  # noqa: F811 - shared fixture
):
    settings, _ = measured
    settings.ha_url = "http://ha.test"
    settings.ha_token = "t"
    settings.grid_power_entity = "sensor.grid_power"
    seed_authority(settings, "observe")
    await seed(settings)
    a = revision()
    monkeypatch.setattr(scheduler, "current_plan", AsyncMock(return_value=a.run))
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    monkeypatch.setattr(scheduler, "_charger_supports_live_rate", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "guard_tick", AsyncMock(return_value=None))
    order = []

    async def guard(*args, **kwargs):
        order.append("guard")

    monkeypatch.setattr(scheduler, "record_guard", guard)

    from ha_spark.energy import readiness_measurements

    real_fetch = readiness_measurements.list_statistic_ids

    async def fetch(*args, **kwargs):
        order.append("day_close")
        return await real_fetch(*args, **kwargs)

    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", fetch)
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    stop = daemon_ticks(monkeypatch, [START + timedelta(days=1, hours=1)])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert order == ["guard", "day_close"]
    (day,) = await ReadinessDays(settings.db_path).days()
    assert day.qualifying, day.reasons
