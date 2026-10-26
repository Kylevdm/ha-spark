"""Slow NESO requests never delay control-loop ticks and are cancelled at shutdown."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import scheduler
from tests.authority_helpers import seed_authority, seed_settings
from tests.test_authority_gate import daemon_ticks
from tests.test_readiness_ledger import START, revision


@respx.mock
async def test_slow_carbon_does_not_delay_control_loop(tmp_path, monkeypatch):
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="Europe/London",
                        db_path=str(tmp_path / "ledger.db"))
    seed_authority(settings, "observe")
    seed_settings(tmp_path, {"carbon_region": 12}, settings=settings)
    run = revision().run
    monkeypatch.setattr(scheduler, "current_plan", AsyncMock(return_value=run))
    soc = AsyncMock(return_value=run.inputs.soc)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", soc)
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    monkeypatch.setattr(scheduler, "record_closed_days", AsyncMock())
    monkeypatch.setattr(scheduler, "readiness_tick", AsyncMock())
    started, cancelled = asyncio.Event(), asyncio.Event()
    requests = []

    async def blocked(request):
        requests.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        side_effect=blocked,
    )
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    stop = daemon_ticks(monkeypatch, [START + timedelta(minutes=m) for m in (0, 1, 2)])
    with pytest.raises(stop):
        await asyncio.wait_for(scheduler.run_forever(settings, poll_seconds=123), timeout=2)
    assert started.is_set()
    assert cancelled.is_set()
    assert soc.await_count == 3
    assert len(requests) == 1
