"""One-way losses only narrow the daemon's DC charging allowance."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ha_spark.devices.execution import execution_context
from ha_spark.devices.inverters.solis import legacy_limits
from ha_spark.energy.supply_guard import SupplyGuard, throttled_rate_w
from ha_spark.energy.topology import BatteryBank
from ha_spark.energy.topology_record import load_topology_record, save_topology_record
from tests.authority_helpers import legacy_settings as Settings
from tests.test_solis_limits import _current_writes, _GuardRest

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8])
@pytest.mark.parametrize("supply", [3000, 18000, 20000, 30000, -3000])
@pytest.mark.parametrize("current", [0, 1000, 3000])
def test_daemon_efficiency_never_widens(eta: float, supply: float, current: float) -> None:
    kwargs = dict(limit_a=75.0, supply_voltage_v=240.0)
    legacy = throttled_rate_w(supply, current, 3000, one_way_charge_efficiency=1.0, **kwargs)
    actual = throttled_rate_w(supply, current, 3000, one_way_charge_efficiency=eta, **kwargs)
    assert 0 <= actual <= legacy
    if eta == 1:
        assert actual == max(0, min(3000, 18000 - supply + current))


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8])
def test_daemon_cut_converts_ac_allowance_to_dc(eta: float) -> None:
    assert throttled_rate_w(
        20000, 3000, 3000, limit_a=75, supply_voltage_v=240,
        one_way_charge_efficiency=eta,
    ) == 1000 * eta


@pytest.mark.parametrize("supply,setpoint,target", [
    (float("nan"), 3000, 3000), (float("-inf"), 3000, 3000),
    (20000, float("inf"), 3000), (20000, 3000, float("inf")),
])
def test_daemon_nonfinite_power_fails_closed(
    supply: float, setpoint: float, target: float,
) -> None:
    assert throttled_rate_w(supply, setpoint, target, limit_a=75,
                            supply_voltage_v=240) == 0


def test_daemon_lossy_restore_needs_ac_request_plus_deadband() -> None:
    kwargs = dict(limit_a=75.0, supply_voltage_v=240.0, one_way_charge_efficiency=0.8)
    # Other load 14 kW: 4 kW available, but the 3 kW DC request needs
    # 3.75 kW AC plus the 200 W deadband. A smaller allowance keeps the cut.
    assert throttled_rate_w(15000, 1000, 3000, **kwargs) == 3000
    assert throttled_rate_w(15100, 1000, 3000, **kwargs) == 1000


@pytest.mark.parametrize("eta", [None, 1.0, 0.8])
async def test_daemon_uses_verified_bank_efficiency_or_legacy_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eta: float | None,
) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    settings = Settings(authority_mode="optimise", db_path=str(tmp_path / "events.db"),
                        grid_power_entity="sensor.grid_power", supply_max_current_a=75,
                        supply_voltage_v=240, battery_voltage_v=50)
    context = execution_context()
    if eta is not None:
        limits = legacy_limits(settings)
        assert limits is not None
        site = limits[0]
        site = site.model_copy(update={"banks": (
            site.banks[0].model_copy(update={"one_way_charge_efficiency": eta}),
        )})
        save_topology_record(settings, 1, site)
        loaded = load_topology_record(settings)
        assert loaded.topology is not None
        assert loaded.topology.banks[0].one_way_charge_efficiency == eta
        monkeypatch.setattr(context, "limits_record", loaded)
    else:
        monkeypatch.setattr(context, "limits_record", None)
    token = context.planned_limits.set(context.limits_key())
    rest = _GuardRest(supply_w=20000, setpoint_a=60)
    try:
        line = await SupplyGuard(settings, rest).tick(3000)  # type: ignore[arg-type]
    finally:
        context.planned_limits.reset(token)
    assert line is not None
    expected_a = int(1000 * (0.949 if eta is None else eta) / 50)
    assert _current_writes(rest) == [expected_a * 10]


@pytest.mark.parametrize("eta", [0, -1, 1.01, float("nan"), float("inf"), True, "0.9"])
def test_addon_bank_rejects_invalid_efficiency(eta: object) -> None:
    with pytest.raises(ValidationError):
        BatteryBank.model_validate({
            "id": "bank", "connection_id": "grid", "usable_capacity_kwh": 10,
            "nominal_voltage_v": 50, "min_soc": 10, "target_cap": 100,
            "one_way_charge_efficiency": eta, "served_load_share_pct": 100,
        })


async def test_daemon_enforces_efficiency_reduction_below_deadband(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    settings = Settings(authority_mode="optimise", db_path=str(tmp_path / "events.db"),
                        grid_power_entity="sensor.grid_power", supply_max_current_a=75,
                        supply_voltage_v=240, battery_voltage_v=50)
    monkeypatch.setattr(execution_context(), "limits_record", None)
    rest = _GuardRest(supply_w=18040, setpoint_a=60)
    line = await SupplyGuard(settings, rest).tick(3000)  # type: ignore[arg-type]
    assert line is not None
    assert _current_writes(rest) == [560]


@pytest.mark.parametrize("eta", [1.0, 0.949, 0.8])
def test_both_guards_agree_on_shared_cut_inputs(eta: float) -> None:
    # Load only the HA-free guard, not the integration package's HA setup.
    path = Path(__file__).parents[1] / "custom_components/ha_spark/supply_guard.py"
    spec = importlib.util.spec_from_file_location("efficiency_guard_under_test", path)
    assert spec is not None and spec.loader is not None
    guard = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = guard
    try:
        spec.loader.exec_module(guard)
    finally:
        del sys.modules[spec.name]
    now = datetime(2026, 10, 29, tzinfo=UTC)
    state = guard.GuardState(restore_day=now.date())
    for seconds in (0, 1):
        at = now + timedelta(seconds=seconds)
        state, _ = guard.step(state, guard.GuardInputs(
            limit_kw=18, requested_kw=3 / eta, reserve_kw=0,
            reading=guard.GuardReading(20000, at),
            measurements=(guard.InverterMeasurement("inv", 3000, at),),
            mirror=guard.AuthorityMirror("optimise", 1, False, False, None),
        ), at)
    assert state.cut_kw == 1
    dc = throttled_rate_w(20000, 3000, 3000, limit_a=75, supply_voltage_v=240,
                          one_way_charge_efficiency=eta)
    assert dc == state.cut_kw * 1000 * eta
