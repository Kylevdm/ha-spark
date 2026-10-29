"""Effective limits at the pure planner boundary."""

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.config import Settings
from ha_spark.devices.inverters.solis import solis_current_a
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import FlexibilityEvent, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.sources import build_config
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.energy.topology import Topology, effective_limits, legacy_topology

NOW = datetime(2026, 10, 29, 23, 30, tzinfo=UTC)


def config(**changes: float) -> PlannerConfig:
    return replace(PlannerConfig(
        capacity_kwh=100, voltage_v=52, min_soc=20, target_cap=90,
        max_current_a=62.5, solar_haircut_k=1, window_start=time(23, 30),
        window_end=time(5, 30), buffer_pct=0, charge_efficiency=0.8,
    ), **changes)


def inputs(*, event: bool = False) -> PlannerInputs:
    return PlannerInputs(
        soc=SocMeasurement(status=SocStatus.OK, observed_at=NOW, value=20,
                           raw_state="20", reported_at=NOW, age_s=0, max_age_s=600),
        solar_tomorrow_kwh=0, predicted_home_load_kwh=100,
        load_slots=(0.5,) * 48, solar_slots=(0.0,) * 48,
        horizon_start=NOW,
        flexibility_event=FlexibilityEvent(
            start=NOW + timedelta(hours=18), end=NOW + timedelta(hours=19),
            direction="export", updated_at=NOW, rate_gbp_kwh=1,
        ) if event else None,
    )


def native(cfg: PlannerConfig, *, bank_a: float = 100, import_a: float = 75) -> Topology:
    raw = legacy_topology(cfg, cfg.driver_kind).model_dump()
    raw.pop("legacy_discharge_kw")
    raw.pop("legacy_v2l")
    raw["banks"][0]["charge_limit_a"] = bank_a
    raw["banks"][0]["discharge_limit_a"] = bank_a
    raw["connections"][0]["import_limit_a"] = import_a
    return Topology.model_validate(raw)


def test_saturated_window_stores_efficiency_adjusted_import_capacity() -> None:
    cfg, inp = config(), inputs()
    topology = native(cfg, import_a=10)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    # (2.4 kW import - 1 kW base load) * 80% * six hours.
    assert plan.required_kwh == pytest.approx(6.72)
    assert plan.required_kwh / plan.charge_efficiency == pytest.approx(8.4)
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", timezone="UTC",
        battery_capacity_kwh=100, battery_voltage_v=52, charge_efficiency=0.8,
    )
    assert solis_current_a(plan.charge_intent, settings) == pytest.approx(8.4 / (6 * 0.052))
    assert plan.hardware_limit_reason is not None
    assert "legacy_connection" in plan.hardware_limit_reason


def test_part_elapsed_import_limited_slot_counts_only_remaining_hours() -> None:
    cfg = config()
    now = NOW + timedelta(minutes=45)
    geometry = compute_horizon_geometry(now, cfg.window_start, cfg.window_end, ZoneInfo("UTC"))
    inp = replace(
        inputs(), now=now, geometry=geometry, horizon_start=geometry.horizon_start,
        load_slots=(0.5,) * len(geometry.slot_starts),
        solar_slots=(0.0,) * len(geometry.slot_starts),
    )
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=native(cfg, import_a=10))
    assert plan.required_kwh == pytest.approx(1.12 * 5.25)


def test_alphaess_config_preserves_owner_planning_limit_without_solis_ceiling() -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", inverter="alphaess",
        battery_capacity_kwh=100, max_charge_current_a=200,
    )
    cfg = build_config(settings, 52)
    inp = replace(inputs(), load_slots=None, solar_slots=None)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert cfg.driver_kind == "alphaess"
    assert plan.required_kwh == pytest.approx(62.4)


def test_bank_limit_names_the_bank() -> None:
    cfg, inp = config(), inputs()
    topology = native(cfg, bank_a=10)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.required_kwh == pytest.approx(3.12)
    assert plan.hardware_limit_reason is not None
    assert "legacy_bank" in plan.hardware_limit_reason


@pytest.mark.parametrize("changes", [
    {"supply_voltage_v": 199.123}, {"supply_max_current_a": 201},
    {"dno_export_limit_kw": 101}, {"capacity_kwh": 0}, {"capacity_kwh": 201},
    {"voltage_v": 11}, {"min_soc": 91}, {"max_current_a": 501},
])
def test_invalid_legacy_limits_fail_closed_without_values(changes: dict[str, float]) -> None:
    cfg, inp = config(**changes), inputs(event=True)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.required_kwh == 0
    assert plan.charge_intent.export is None
    assert plan.effective_limits is None
    assert plan.topology is None
    assert plan.limits_reason is not None
    assert "199" not in plan.limits_reason


def test_efficiency_above_one_fails_closed() -> None:
    cfg, inp = config(charge_efficiency=1.1), inputs(event=True)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.required_kwh == 0
    assert plan.charge_intent.export is None
    assert plan.limits_reason is not None


def test_supplied_effective_limits_are_recorded() -> None:
    cfg, inp = config(), inputs()
    topology = native(cfg)
    limits = effective_limits(topology)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology, limits=limits,
                        major_loads=())
    assert plan.topology == topology
    assert plan.effective_limits == limits
    assert plan.limits_source == "legacy"


def test_export_is_capped_by_bank_below_sum_of_inverters() -> None:
    cfg, inp = config(), inputs(event=True)
    inp = replace(inp, soc=replace(inp.soc, value=90))
    raw = native(cfg).model_dump()
    raw["banks"][0]["discharge_limit_a"] = 40
    inverter = {**raw["inverters"][0], "driver_kind": "alphaess", "discharge_limit_a": 30}
    raw["inverters"] = (inverter, {**inverter, "id": "second_inverter"})
    topology = Topology.model_validate(raw)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    export = plan.charge_intent.export
    assert export is not None
    assert export.slot_export_kw == pytest.approx((1.08, 1.08))  # 40 A * 52 V - 1 kW house
    assert plan.effective_limits is not None
    assert plan.effective_limits.banks[0].discharge_kw == pytest.approx(2.08)


@pytest.mark.parametrize("ceiling", ["inverter", "bank", "combined_bank"])
def test_fixed_command_refused_at_effective_discharge_ceiling(ceiling: str) -> None:
    cfg, inp = config(dno_export_limit_kw=10), inputs(event=True)
    inp = replace(inp, soc=replace(inp.soc, value=90))
    raw = native(cfg).model_dump()
    if ceiling == "inverter":
        raw["inverters"][0]["discharge_limit_a"] = 60
    elif ceiling == "bank":
        raw["banks"][0]["discharge_limit_a"] = 60
    else:
        raw["inverters"] = (*raw["inverters"], {**raw["inverters"][0], "id": "second"})
        raw["banks"][0]["discharge_limit_a"] = 100
    topology = Topology.model_validate(raw)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.charge_intent.export is None
    assert {skip.reason for skip in plan.export_skips} == {
        "Skipped paid slot: fixed export command exceeds the effective discharge ceiling."
    }


def test_legacy_export_at_52v_keeps_exact_kw_ceiling() -> None:
    cfg, inp = config(), inputs(event=True)
    inp = replace(inp, soc=replace(inp.soc, value=90))
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    export = plan.charge_intent.export
    assert export is not None
    assert export.slot_export_kw == pytest.approx((2.2, 2.2))


def test_export_intent_carries_site_export_not_dno_permission() -> None:
    cfg, inp = config(dno_export_limit_kw=7.36, supply_max_current_a=15), inputs(event=True)
    inp = replace(inp, soc=replace(inp.soc, value=90))
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    export = plan.charge_intent.export
    assert export is not None
    assert export.dno_export_limit_kw == pytest.approx(3.6)


def test_inverter_limit_and_driver_ceiling_name_inverter() -> None:
    cfg, inp = config(max_current_a=200), inputs()
    inp = replace(inp, load_slots=(0.5,) * 12 + (1.0,) * 36)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.required_kwh == pytest.approx(19.5)
    assert plan.hardware_limit_reason == "Charging limited by hardware: legacy_inverter."


def test_daily_model_keeps_dc_current_times_hours_without_import_cap() -> None:
    cfg, inp = config(supply_max_current_a=1), inputs()
    inp = replace(inp, load_slots=None, solar_slots=None)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.required_kwh == pytest.approx(19.5)


@pytest.mark.parametrize("invalid", [False, True])
def test_zero_limits_never_raise_the_charge_target_after_pre_window_drain(invalid: bool) -> None:
    cfg = config(supply_voltage_v=199 if invalid else 240)
    now = NOW.replace(hour=16, minute=0)
    geometry = compute_horizon_geometry(now, cfg.window_start, cfg.window_end, ZoneInfo("UTC"))
    inp = replace(
        inputs(), now=now, geometry=geometry, horizon_start=geometry.horizon_start,
        load_slots=(0.5,) * len(geometry.slot_starts),
        solar_slots=(0.0,) * len(geometry.slot_starts),
    )
    topology = None if invalid else native(cfg, bank_a=0)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.required_kwh == 0
    assert plan.charge_intent.expected_window_start_soc is not None
    assert plan.target_soc <= plan.charge_intent.expected_window_start_soc


def test_unsupported_topology_fails_closed() -> None:
    cfg, inp = config(), inputs(event=True)
    raw = native(cfg).model_dump()
    raw["banks"][0]["served_load_share_pct"] = 50
    raw["banks"] = (*raw["banks"], {**raw["banks"][0], "id": "second_bank"})
    topology = Topology.model_validate(raw)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.required_kwh == 0
    assert plan.charge_intent.export is None
    assert plan.limits_reason is not None


def test_zero_charge_limit_keeps_a_lower_capped_target() -> None:
    """The zero-capacity branch only narrows: SoC 95% above a 90% cap keeps 90% (#536)."""
    cfg, inp = config(max_current_a=0), inputs()
    soc = replace(inp.soc, value=95, raw_state="95")
    inp = replace(inp, soc=soc)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.required_kwh == 0
    assert plan.target_soc == pytest.approx(90)
