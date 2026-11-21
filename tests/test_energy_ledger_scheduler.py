"""Exercise the ledger on daemon boundary ticks, independently of control."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.energy import scheduler
from ha_spark.energy.energy_ledger import EnergyLedger
from tests.authority_helpers import run_daemon, seed_authority
from tests.test_authority_gate import daemon_ticks
from tests.test_energy_ledger import CLOSE, HALF_HOUR, counter, settings_for
from tests.test_readiness_ledger import START, revision

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


def stub_control(monkeypatch):
    a = revision()
    b = revision("B", 0.4)
    inputs = replace(b.run.inputs, horizon_start=CLOSE, now=CLOSE)
    b = replace(b, issued_at=CLOSE, run=replace(b.run, inputs=inputs))
    monkeypatch.setattr(scheduler, "run_once", AsyncMock(side_effect=[
        scheduler.RunResult(r.run.plan, None, revision=r) for r in (a, b)
    ]))
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_planned_rate_w", AsyncMock(return_value=None))
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    monkeypatch.setattr(scheduler, "record_closed_days", AsyncMock())
    monkeypatch.setattr(scheduler, "readiness_tick", AsyncMock())
    monkeypatch.setattr(scheduler, "backfill_energy_ledger", AsyncMock(return_value=True))
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})


def wait_at_tick_end(monkeypatch, ready, *, clear=False):
    """Synchronise the scripted clock with the worker, not a production control step."""
    advance = asyncio.sleep

    async def sleep(seconds):
        if seconds == 123:
            await ready.wait()
            if clear:
                ready.clear()
        await advance(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)


@pytest.mark.parametrize("site", ["observe", "optimise", "retired"])
@respx.mock
async def test_daemon_records_closed_slot_using_pre_replan_revision(tmp_path, monkeypatch, site):
    settings = settings_for(tmp_path, timezone="Europe/London")
    authority = seed_authority(settings, "optimise" if site == "optimise" else "observe")
    if site == "retired":
        assert authority.handover("pending_integration") == "retired"
    counter("sensor.grid_import", ("10", START), ("12", CLOSE))
    stub_control(monkeypatch)
    ledger = EnergyLedger(settings.db_path)

    finished = asyncio.Event()
    record = EnergyLedger.record_boundary

    async def record_and_signal(self, *args):
        await record(self, *args)
        finished.set()

    monkeypatch.setattr(EnergyLedger, "record_boundary", record_and_signal)
    tz = ZoneInfo(settings.timezone)
    stop = daemon_ticks(monkeypatch, [START.astimezone(tz), CLOSE.astimezone(tz)])
    wait_at_tick_end(monkeypatch, finished, clear=True)
    with pytest.raises(stop):
        await asyncio.wait_for(run_daemon(settings, poll_seconds=123), timeout=3)
    [row] = await ledger.slots(START, CLOSE)
    assert row.components["grid_import"].kwh == row.house_load_kwh == 2
    assert (row.import_rate, row.revision_id) == (0.1, "A")
    assert not any("/api/services/" in str(c.request.url) for c in respx.calls)


@respx.mock
async def test_blocked_counter_reader_never_delays_guard_or_queues_next_slot(tmp_path, monkeypatch):
    settings = settings_for(tmp_path, timezone="Europe/London", grid_power_entity="sensor.grid")
    seed_authority(settings, "observe")
    started, cancelled = asyncio.Event(), asyncio.Event()

    reads = []

    async def blocked(request):
        reads.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(404)

    respx.get("http://ha.test/api/states/sensor.grid_import").mock(side_effect=blocked)
    stub_control(monkeypatch)
    guard = AsyncMock(return_value=None)
    monkeypatch.setattr(scheduler, "guard_tick", guard)
    monkeypatch.setattr(scheduler, "_charger_supports_live_rate", AsyncMock(return_value=True))

    tz = ZoneInfo(settings.timezone)
    ticks = (START, START + timedelta(minutes=1), CLOSE)
    stop = daemon_ticks(monkeypatch, [tick.astimezone(tz) for tick in ticks])
    wait_at_tick_end(monkeypatch, started)
    with pytest.raises(stop):
        await asyncio.wait_for(run_daemon(settings, poll_seconds=123), timeout=3)
    assert guard.await_count == 3
    assert started.is_set() and cancelled.is_set()
    assert len(reads) == 1
    assert await EnergyLedger(settings.db_path).slots(START - HALF_HOUR, CLOSE) == []
    assert not any("/api/services/" in str(c.request.url) for c in respx.calls)
