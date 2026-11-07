"""Native discharge ceiling warnings use accepted effective limits, never live I/O."""

from unittest.mock import AsyncMock, Mock

import pytest

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import (
    LoadedTopology,
    load_topology_record,
    save_topology_record,
)


def topology(*, confirmed=True, transport="native", discharge=62.5, voltage=51.2):
    return Topology.model_validate({
        "connections": [{"id": "grid", "supply_voltage_v": 230.0}],
        "banks": [{"id": "bank", "connection_id": "grid", "usable_capacity_kwh": 10.0,
                   "nominal_voltage_v": voltage, "min_soc": 10.0, "target_cap": 100.0,
                   "served_load_share_pct": 100.0}],
        "inverters": [{"id": "main", "bank_id": "bank", "driver_kind": "solis",
                       "transport": transport, "gateway_host": "PRIVATE-HOST",
                       "confirmed": confirmed, "discharge_limit_a": discharge}],
    })


def expected_warning(ceiling="3.00", amps=58):
    return health.CheckResult(
        "Discharge ceiling", health.Status.WARN,
        "native discharge 3.20 kW (main) is above battery_discharge_ceiling_kw "
        f"{ceiling} kW; set its DC discharge limit to {amps} A to keep the lower ceiling",
    )


def test_lower_ceiling_warns_without_failing():
    rows = health.check_discharge_ceiling(
        Settings(battery_discharge_ceiling_kw=3.0), LoadedTopology("native", 1, topology()),
    )
    assert rows == [expected_warning()]
    assert health.exit_code([health.CheckResult("HA REST", health.Status.OK, ""), *rows]) == 2
    assert "PRIVATE-HOST" not in health.format_report(rows)


@pytest.mark.parametrize("settings", [
    Settings(battery_discharge_ceiling_kw=3.3),
    Settings(battery_discharge_ceiling_kw=3.2),
    Settings(),
])
def test_no_warning_for_higher_equal_or_default_ceiling(settings):
    assert health.check_discharge_ceiling(settings, LoadedTopology("native", 1, topology())) == []


@pytest.mark.parametrize("loaded", [
    LoadedTopology("legacy"),
    LoadedTopology("unavailable"),
    LoadedTopology("native"),
    LoadedTopology("native", 1, topology(confirmed=False)),
    LoadedTopology("native", 1, topology(transport="legacy")),
    LoadedTopology("native", 1, topology(discharge=50.0)),
])
def test_no_warning_without_native_effective_excess(loaded):
    assert health.check_discharge_ceiling(Settings(battery_discharge_ceiling_kw=3.0), loaded) == []


def test_default_g98_ceiling_is_compared_when_never_set():
    assert health.check_discharge_ceiling(
        Settings(), LoadedTopology("native", 1, topology(voltage=64.0)),
    ) == [health.CheckResult(
        "Discharge ceiling", health.Status.WARN,
        "native discharge 4.00 kW (main) is above battery_discharge_ceiling_kw "
        "3.68 kW; set its DC discharge limit to 57 A to keep the lower ceiling",
    )]


def test_explicit_zero_ceiling_is_compared():
    assert health.check_discharge_ceiling(
        Settings(battery_discharge_ceiling_kw=0), LoadedTopology("native", 1, topology()),
    ) == [expected_warning("0.00", 0)]


def test_driver_ceiling_caps_comparison():
    assert health.check_discharge_ceiling(
        Settings(battery_discharge_ceiling_kw=3.3),
        LoadedTopology("native", 1, topology(discharge=100.0)),
    ) == []


def test_invalid_effective_limits_leave_warning_to_existing_checks(monkeypatch):
    monkeypatch.setattr(health, "effective_limits", Mock(side_effect=ValueError))
    assert health.check_discharge_ceiling(
        Settings(battery_discharge_ceiling_kw=3.0), LoadedTopology("native", 1, topology()),
    ) == []


async def test_run_health_reads_durable_discharge_limit(tmp_path, monkeypatch):
    settings = Settings(db_path=str(tmp_path / "spark.db"), battery_discharge_ceiling_kw=3.0)
    save_topology_record(settings, 1, topology())
    assert health.check_discharge_ceiling(settings, load_topology_record(settings)) == [
        expected_warning(),
    ]
    for name in (
        "check_operating_mode", "check_integration_link", "check_authority_owner",
        "check_site_pairing", "check_native_soc", "check_ha_rest",
        "check_ha_websocket", "check_ollama", "check_sqlite", "check_load_history",
        "check_supply_guard", "check_limits", "check_topology_record",
        "check_tariff_provider", "check_export_event_provider", "check_ha_timezone",
    ):
        monkeypatch.setattr(health, name, AsyncMock(return_value=health.CheckResult(
            name, health.Status.OK, "",
        )))
    monkeypatch.setattr(health, "check_native_solis_transports", AsyncMock(return_value=[]))
    monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
    monkeypatch.setattr(health, "check_entity_config", lambda _: health.CheckResult(
        "Entities", health.Status.OK, "",
    ))
    monkeypatch.setattr(health, "check_hold_release", lambda _: [])
    rows = await health.run_health(settings)
    assert [r for r in rows if r.name == "Discharge ceiling"] == [expected_warning()]
    assert health.exit_code(rows) == 2
