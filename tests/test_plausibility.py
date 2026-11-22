"""The one site bound behind every ledger write's plausibility rule."""

from pathlib import Path

import pytest

from ha_spark.config import DeviceConfig, Settings
from ha_spark.energy.plausibility import DEFAULT_PLAUSIBLE_KW, plausible, plausible_kw
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import save_topology_record

MARKER = "ha_spark_topology.initialised"


def settings_for(tmp_path, **kwargs):
    return Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ledger.db"),
        **kwargs,
    )


def test_legacy_settings_bound_is_the_supply_limit_plus_a_quarter(tmp_path):
    settings = settings_for(tmp_path, supply_max_current_a=60, supply_voltage_v=240)
    assert plausible_kw(settings) == pytest.approx(60 * 240 / 1000 * 1.25)


def test_a_value_just_under_the_bound_passes_and_just_over_fails(tmp_path):
    bound = plausible_kw(settings_for(tmp_path, supply_max_current_a=60, supply_voltage_v=240))
    assert plausible(bound * 0.5 - 0.001, 1800.0, bound)
    assert not plausible(bound * 0.5 + 0.001, 1800.0, bound)
    assert plausible(float("nan"), 1800.0, bound) is False


def test_no_usable_limit_falls_back_to_30_kw(tmp_path):
    settings = settings_for(tmp_path, supply_max_current_a=0, dno_export_limit_kw=0)
    assert plausible_kw(settings) == DEFAULT_PLAUSIBLE_KW


def test_unreadable_accepted_topology_falls_back_to_30_kw(tmp_path):
    settings = settings_for(tmp_path)
    parent = Path(settings.db_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    (parent / MARKER).write_text("{}")
    assert plausible_kw(settings) == DEFAULT_PLAUSIBLE_KW


def test_native_limits_bound_the_ledger_writes(tmp_path):
    settings = settings_for(tmp_path, devices=[DeviceConfig(id="legacy", driver="solis")])
    save_topology_record(settings, 1, Topology.model_validate({
        "connections": [{"id": "grid", "supply_voltage_v": 240.0, "import_limit_a": 80.0,
                         "export_limit_a": 80.0, "dno_export_limit_kw": 3.68}],
        "banks": [{"id": "bank", "connection_id": "grid", "usable_capacity_kwh": 100.0,
                   "nominal_voltage_v": 52.0, "min_soc": 20.0, "target_cap": 90.0,
                   "charge_limit_a": 100.0, "discharge_limit_a": 100.0,
                   "served_load_share_pct": 100.0}],
        "inverters": [{"id": "inv", "bank_id": "bank", "driver_kind": "solis",
                       "charge_limit_a": 40.0, "discharge_limit_a": 62.5, "confirmed": True}],
    }))
    assert plausible_kw(settings) == pytest.approx(80 * 240 / 1000 * 1.25)


def test_plausibility_module_has_no_ha_write_path():
    from tests.test_energy_ledger import test_statistics_modules_have_no_write_path

    test_statistics_modules_have_no_write_path("plausibility")
