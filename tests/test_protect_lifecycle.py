"""Protect remains hands-off charging through sustained SoC failure."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from ha_spark.authority import load_authority
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from tests.authority_helpers import seed_authority
from tests.test_authority_gate import daemon_ticks, hardware_calls
from tests.test_authority_gate import ha as ha
from tests.test_authority_gate import settings as settings


async def test_protect_sustained_soc_failure_only_allows_clock(
    settings, ha, monkeypatch,
):
    settings = settings.model_copy(update={
        "soc_entity": "sensor.soc", "grid_power_entity": "sensor.grid",
        "inverter_clock_dst_sync": True, "soc_failure_threshold": 2,
    })
    seed_authority(settings, "protect")
    stop = daemon_ticks(monkeypatch, [datetime(2026, 6, 10, 23, minute)
                                     for minute in range(30, 35)])
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24, slots=None, source="test"),
    ))
    monkeypatch.setattr(scheduler, "sample_signals", AsyncMock())
    monkeypatch.setattr(scheduler.DstClockSync, "due", lambda *_: datetime.now(UTC))
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    writes = hardware_calls(ha)
    assert writes
    assert all(domain == "modbus" and service == "write_register" and data["address"] == 43000
               for domain, service, data in writes)
    authority = load_authority(settings).state
    assert authority.committed_mode == "protect"
    assert not authority.fallback.active
