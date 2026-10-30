"""Read-only legacy Solis transport diagnostics."""

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ha_spark import health
from ha_spark.config import Settings

FIELDS = (
    "timed_charge_current", "timed_discharge_current", "work_mode_bitfield",
    "timed_charge_start_hours", "timed_charge_start_minutes",
    "timed_charge_end_hours", "timed_charge_end_minutes",
    "timed_discharge_start_hours", "timed_discharge_start_minutes",
    "timed_discharge_end_hours", "timed_discharge_end_minutes",
)


def settings(hub="solis_control"):
    return Settings(ha_url="http://ha.test", ha_token="CANARY", solis_control_hub=hub)


@respx.mock
async def test_legacy_transport_ok_warns_2027_10():
    for field in FIELDS:
        entity = f"sensor.solis_control_{field}"
        respx.get(f"http://ha.test/api/states/{entity}").respond(
            200, json={"entity_id": entity, "state": "0"},
        )
    result = await health.check_legacy_solis_transport(settings(), "main_inverter")
    assert result.name == "Solis transport (main_inverter)"
    assert result.status is health.Status.OK
    assert "warning" in result.detail.lower()
    assert "HA 2027.10" in result.detail
    assert health.exit_code([result]) == 0
    assert all(call.request.method == "GET" for call in respx.calls)


@pytest.mark.parametrize("bad", [None, "unavailable", "unknown", "nan", "inf", "bad"])
@pytest.mark.parametrize("field", FIELDS)
@respx.mock
async def test_legacy_transport_sensor_unreadable_fails(bad, field):
    for key in FIELDS:
        entity = f"sensor.solis_control_{key}"
        response = httpx.Response(404) if key == field and bad is None else httpx.Response(
            200, json={"entity_id": entity, "state": bad if key == field else "0"},
        )
        respx.get(f"http://ha.test/api/states/{entity}").mock(return_value=response)
    result = await health.check_legacy_solis_transport(settings(), "main_inverter")
    assert result.status is health.Status.FAIL
    assert result.detail == f"overlay hub missing: sensor.solis_control_{field}"
    assert "CANARY" not in result.detail
    assert health.exit_code([result]) == 2


@respx.mock
async def test_empty_hub_no_control_path():
    result = await health.check_legacy_solis_transport(settings(""), "main_inverter")
    assert result.status is health.Status.FAIL
    assert result.detail == "no control path"
    assert not respx.calls


@pytest.mark.parametrize("inverter", ["solis", "alphaess"])
@respx.mock
async def test_empty_hub_skips_inverter_clock_and_alphaess_has_no_transport_row(
    monkeypatch, inverter,
):
    # Isolate device diagnostics from unrelated health probes, all without I/O.
    for name in (
        "check_operating_mode", "check_integration_link", "check_ha_rest",
        "check_ha_websocket", "check_ollama", "check_sqlite", "check_load_history",
        "check_supply_guard", "check_limits", "check_topology_record",
        "check_tariff_provider", "check_export_event_provider", "check_ha_timezone",
    ):
        monkeypatch.setattr(health, name, AsyncMock(return_value=health.CheckResult(
            name, health.Status.OK, "",
        )))
    monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
    results = await health.run_health(Settings(
        ha_url="http://ha.test", ha_token="CANARY", inverter=inverter,
    ))
    assert not any(r.name == "Inverter clock" for r in results)
    rows = [r for r in results if r.name.startswith("Solis transport")]
    assert len(rows) == (1 if inverter == "solis" else 0)
    assert not respx.calls
