"""Native transport health consumes only read-only daemon diagnostics."""

from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import LoadedTopology

REASONS = (
    "unvalidated_identity", "identity_read_failed", "v2_tou_firmware", "caching_on",
    "gateway_not_owned", "unreachable",
)


@pytest.mark.parametrize(("validation", "reason", "status", "detail"), [
    ("validated", None, health.Status.OK, "validated"),
    *(("read_only", reason, health.Status.WARN, reason) for reason in REASONS),
    ("read_only", "SECRET-CANARY", health.Status.WARN, "validation unknown"),
    ("unexpected", None, health.Status.WARN, "validation unknown"),
])
async def test_native_transport_validation(monkeypatch, validation, reason, status, detail):
    submit = AsyncMock(return_value=Reply(integration_link={
        "state": "connected", "devices": [{
            "id": "main", "transport": "native", "validation": validation, "reason": reason,
        }],
    }))
    monkeypatch.setattr(health, "submit_socket", submit)
    settings = Settings()
    rows = await health.check_native_solis_transports(settings, ("main",))
    assert rows == [health.CheckResult("Solis transport (main)", status, detail)]
    submit.assert_awaited_once_with(settings, "integration_link")
    assert "SECRET-CANARY" not in health.format_report(rows)


@pytest.mark.parametrize("reply", [None, Reply(), Reply(error="SECRET-CANARY"),
                                  Reply(integration_link={"devices": []}),
                                  Reply(integration_link={"devices": None})])
async def test_native_transport_unknown(monkeypatch, reply):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=reply))
    rows = await health.check_native_solis_transports(Settings(), ("main", "second"))
    assert len(rows) == 2
    assert all(r.status is health.Status.WARN and r.detail == "validation unknown" for r in rows)


async def test_native_transport_connection_lost(monkeypatch):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(side_effect=OSError("SECRET-CANARY")))
    rows = await health.check_native_solis_transports(Settings(), ("main",))
    assert rows[0].detail == "validation unknown"
    assert rows[0].status is health.Status.WARN


async def test_native_transport_overlay_inconsistent(monkeypatch):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=None))
    rows = await health.check_native_solis_transports(
        Settings(solis_control_hub="overlay"), ("main",),
    )
    assert rows == [health.CheckResult(
        "Solis transport (main)", health.Status.FAIL, "inconsistent",
    )]
    assert health.exit_code(rows) == 2


async def test_run_health_native_replaces_legacy_row(monkeypatch):
    for name in (
        "check_operating_mode", "check_integration_link", "check_authority_owner", "check_ha_rest",
        "check_ha_websocket", "check_ollama", "check_sqlite", "check_load_history",
        "check_supply_guard", "check_limits", "check_topology_record",
        "check_tariff_provider", "check_export_event_provider", "check_ha_timezone",
    ):
        monkeypatch.setattr(health, name, AsyncMock(return_value=health.CheckResult(
            name, health.Status.OK, "",
        )))
    monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
    legacy = AsyncMock(side_effect=AssertionError("legacy probe must not run"))
    monkeypatch.setattr(health, "check_legacy_solis_transport", legacy)
    topology = Topology.model_validate({
        "inverters": [{"id": "main_inverter", "driver_kind": "solis", "transport": "native",
                       "gateway_host": "PRIVATE-HOST", "bank_id": "bank"}],
        "banks": [{"id": "bank", "connection_id": "grid", "usable_capacity_kwh": 10.0,
                   "nominal_voltage_v": 50.0, "min_soc": 10.0, "target_cap": 100.0,
                   "served_load_share_pct": 100.0}],
        "connections": [{"id": "grid", "supply_voltage_v": 230.0}],
    })
    monkeypatch.setattr(health, "load_topology_record", lambda _: LoadedTopology(
        kind="native", topology=topology,
    ))
    submit = AsyncMock(return_value=None)
    monkeypatch.setattr(health, "submit_socket", submit)
    rows = await health.run_health(Settings(devices=[
        {"id": "main_inverter", "type": "inverter", "driver": "solis"},
    ]))
    native = [r for r in rows if r.name.startswith("Solis transport")]
    assert native == [health.CheckResult(
        "Solis transport (main_inverter)", health.Status.WARN, "validation unknown",
    )]
    submit.assert_awaited_once()
    assert "PRIVATE-HOST" not in health.format_report(rows)
