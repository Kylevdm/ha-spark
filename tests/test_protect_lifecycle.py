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


async def test_protect_never_writes_export_even_with_a_live_export(tmp_path, monkeypatch):
    from dataclasses import replace

    from ha_spark.energy.export_store import ExportEventStore
    from tests.test_solis_power_switch import FakeRest, _device, _export, _intent

    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    rest = FakeRest(switch="On")
    # A resident export half and a live verified export record, plus a planned new export.
    rest.states["sensor.solis_control_timed_discharge_start_hours"] = "17"
    rest.states["sensor.solis_control_timed_discharge_end_hours"] = "18"
    device = _device(rest, tmp_path, authority_mode="protect")
    async with ExportEventStore(device._settings.db_path) as store:
        await store.save("export|live", datetime.now(UTC).replace(year=2099))
    intent = replace(_intent(export=_export()), export_trusted=True)

    lines = [*await device.apply(intent), *await device.write_safe_state()]
    lines += await device.apply(replace(intent, export=None))

    assert rest.calls == []  # no select, switch or register write of any kind
    assert lines
