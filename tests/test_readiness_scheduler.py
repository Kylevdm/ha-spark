"""Readiness capture through the daemon, including failed and mid-slot replans."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
import respx

from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Pending, Request
from ha_spark.energy import scheduler
from ha_spark.energy.readiness_ledger import ReadinessLedger
from tests.authority_helpers import seed_authority
from tests.test_authority_gate import daemon_ticks
from tests.test_readiness_ledger import START, revision


@respx.mock
async def test_daemon_records_in_force_revision_after_mid_slot_request_and_failed_replan(
    tmp_path, monkeypatch,
):
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="Europe/London",
                        db_path=str(tmp_path / "ledger.db"), grid_power_entity="sensor.grid")
    seed_authority(settings, "observe")
    a, b = revision(), revision("B", 0.2)
    b = replace(b, run=replace(b.run, schedule=replace(
        b.run.schedule, prices=(0.2, 0.25) + (0.3,) * 46,
    )))
    computations = 0

    async def compute(*args, **kwargs):
        nonlocal computations
        computations += 1
        if computations == 3:
            raise RuntimeError("planner unavailable at boundary")
        return a.run if computations == 1 else b.run

    monkeypatch.setattr(scheduler, "current_plan", compute)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    # A daemon request arrives on the second tick, mid-slot.
    polls = 0

    def next_request(self):
        nonlocal polls
        polls += 1
        if polls == 2:
            return Pending(Request(op="replan"), asyncio.get_running_loop().create_future())
        return None

    monkeypatch.setattr(DaemonChannel, "next_request", next_request)
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "20000", "attributes": {}},
    )
    respx.get("http://ha.test/api/states/sensor.solis_control_timed_charge_current").respond(
        200, json={"entity_id": "sensor.solis_control_timed_charge_current",
                   "state": "20", "attributes": {}},
    )
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    ticks = [START + timedelta(minutes=m) for m in (0, 10, 30, 60)]
    stop = daemon_ticks(monkeypatch, [t.astimezone(ZoneInfo(settings.timezone)) for t in ticks])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    rows = await ReadinessLedger(settings.db_path).slots(START, START + timedelta(hours=2))
    assert len(rows) == 3
    assert rows[0].issued_at == START
    assert rows[0].import_rate == 0.1
    assert rows[1].issued_at == START + timedelta(minutes=10)
    assert rows[1].revision_id != rows[0].revision_id
    assert rows[1].import_rate == 0.25  # previous revision's index 1, NOT index 0
    assert rows[2].issued_at == START + timedelta(hours=1)
    assert computations == 4
    guards = await ReadinessLedger(settings.db_path).guards()
    assert len(guards) == 4
    assert all(g.sample_valid and g.breach for g in guards)
    assert not any("/api/services/" in str(c.request.url) for c in respx.calls)
