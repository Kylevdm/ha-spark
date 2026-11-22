"""A slow meter-history request never holds up the daemon's protection ticks."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.energy import scheduler
from tests.authority_helpers import run_daemon, seed_authority
from tests.test_authority_gate import daemon_ticks
from tests.test_energy_ledger import CLOSE, settings_for
from tests.test_energy_ledger_scheduler import stub_control, wait_at_tick_end
from tests.test_meter_pull import API, URL
from tests.test_readiness_ledger import START

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


@respx.mock
async def test_slow_meter_pull_does_not_delay_guard_or_queue_another_pull(tmp_path, monkeypatch):
    settings = settings_for(
        tmp_path, timezone="Europe/London", grid_power_entity="sensor.grid",
        octopus_api_url=API, octopus_api_key="sk_test",
        octopus_mpan="test-point", octopus_meter_serial="test-meter",
    )
    seed_authority(settings, "observe")
    stub_control(monkeypatch)
    started, cancelled = asyncio.Event(), asyncio.Event()
    requests = []

    async def blocked(request):
        requests.append(request)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(503)

    respx.get(URL).mock(side_effect=blocked)
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
    assert cancelled.is_set()
    assert len(requests) == 1
