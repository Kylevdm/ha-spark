"""Regression coverage for slot provenance at the daemon recording boundary."""
import asyncio
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


@pytest.mark.parametrize("inverter", ["solis", "alphaess"])
@respx.mock
async def test_capture_uses_post_handover_mode_and_driver_rate_capability(
    tmp_path, monkeypatch, inverter,
):
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="Europe/London",
                        db_path=str(tmp_path / "ledger.db"), inverter=inverter,
                        grid_power_entity="sensor.grid")
    seed_authority(settings, "protect")
    a = revision()
    monkeypatch.setattr(scheduler, "current_plan", AsyncMock(return_value=a.run))
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=a.run.inputs.soc))
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())
    monkeypatch.setattr(DaemonChannel, "next_request", lambda self: Pending(
        Request(op="mode", target="observe", expected_epoch=2),
        asyncio.get_running_loop().create_future(),
    ))
    respx.get("http://ha.test/api/states/sensor.grid").respond(
        200, json={"entity_id": "sensor.grid", "state": "500", "attributes": {}},
    )
    respx.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
    respx.post(url__regex=r"http://ha.test/api/states/.*").respond(200, json={})
    stop = daemon_ticks(monkeypatch, [START.astimezone(ZoneInfo(settings.timezone))])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    (row,) = await ReadinessLedger(settings.db_path).slots(START, START + timedelta(hours=1))
    assert row.operating_mode == "observe"
    if inverter == "alphaess":
        assert row.charge_current_a is None
    else:
        assert row.charge_current_a is not None
        assert len(await ReadinessLedger(settings.db_path).guards()) == 1
    assert not any("/api/services/" in str(c.request.url) for c in respx.calls)
