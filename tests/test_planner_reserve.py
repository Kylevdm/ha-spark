"""Major-load capacity at the pure planner boundary."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ha_spark.energy.models import ChargePlan, DispatchSlot
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.energy.topology import MajorLoadObservation, Topology
from tests.test_planner_limits import NOW, config, inputs, native


def site(
    *, marked: bool = True, metered: bool = True, import_a: float = 40, bank_a: float = 100,
) -> Topology:
    raw = native(config(), import_a=import_a, bank_a=bank_a).model_dump()
    raw["major_loads"] = [
        {"id": "ev", "connection_id": "legacy_connection", "kind": "ev_charger",
         "demand_kw": 7.4, "dispatch_driven": marked},
        {"id": "hp", "connection_id": "legacy_connection", "kind": "heat_pump",
         "heating_demand_kw": 3, "cooling_demand_kw": 2,
         "power_entity": "sensor.hp" if metered else None},
    ]
    return Topology.model_validate(raw)


def plan_for(
    topology: Topology, observations: tuple[MajorLoadObservation, ...] = (),
) -> ChargePlan:
    cfg, inp = config(), replace(inputs(), now=NOW)
    return compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology,
                        major_loads=observations)


def test_shared_reserve_fixture_sizes_planner_capacity() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "limits" /
                          "planner_and_guard_reserve.json").read_text())
    topology = Topology.model_validate(fixture["topology"])
    cfg, inp = config(), inputs()
    inp = replace(inp, load_slots=(0.5,) * 12 + (1.0,) * 36)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.required_kwh == pytest.approx((18 - 1 - 10.4) * 0.8 * 6)


def test_idle_ev_and_heat_pump_reserve_formula() -> None:
    plan = plan_for(site())
    # 9.6 kW supply - 1 kW base - max(7.4, (7.4 + 3)/2), then efficiency.
    assert plan.required_kwh == pytest.approx(1.2 * 0.8 * 6)


def test_running_heat_pump_counts_draw_once_and_leaves_reserve() -> None:
    plan = plan_for(site(), (MajorLoadObservation(load_id="hp", reading_kw=0.5,
                                                reported_at=NOW),))
    assert plan.required_kwh == pytest.approx(0.7 * 0.8 * 6)


@pytest.mark.parametrize("reading,reported", [
    (0.5, NOW - timedelta(seconds=61)), (-1, NOW),
    (0.5, NOW + timedelta(seconds=1)), (0.5, NOW.replace(tzinfo=None)),
    (float("nan"), NOW), (None, None), (0.29, NOW),
])
def test_bad_reading_stays_in_reserve(reading, reported) -> None:
    assert plan_for(site(), (MajorLoadObservation(load_id="hp", reading_kw=reading,
                                                 reported_at=reported),)) == plan_for(site())


@pytest.mark.parametrize("now", [None, NOW.replace(tzinfo=None)])
def test_missing_or_naive_now_keeps_load_in_reserve(now) -> None:
    cfg, inp = config(), replace(inputs(), now=now)
    topology = site()
    assert compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology,
                        major_loads=(MajorLoadObservation(load_id="hp", reading_kw=0.5,
                                                         reported_at=NOW),)) == compute_plan(
        inp, cfg, fixed_schedule(inp, cfg), topology=topology)


def test_unknown_observation_is_ignored() -> None:
    assert plan_for(site(), (MajorLoadObservation(load_id="unknown", reading_kw=40,
                                                reported_at=NOW),)) == plan_for(site())


def test_unmetered_load_cannot_be_running() -> None:
    assert plan_for(site(metered=False), (MajorLoadObservation(
        load_id="hp", reading_kw=0.5, reported_at=NOW),)) == plan_for(site(metered=False))


@pytest.mark.parametrize("marked", [False, True])
def test_only_marked_ev_is_expected_in_dispatch_slots(marked: bool) -> None:
    cfg, inp = config(), replace(inputs(), now=NOW)
    inp = replace(inp, dispatches=(DispatchSlot(start=NOW, end=NOW + timedelta(hours=1),
                                               charge_in_kwh=1),))
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=site(marked=marked))
    # Expected EV + idle HP reserve exhausts import in the first hour only.
    assert plan.required_kwh == pytest.approx(1.2 * 0.8 * (5 if marked else 6))


def test_running_expected_ev_counts_only_its_draw() -> None:
    cfg, inp = config(), replace(inputs(), now=NOW)
    inp = replace(inp, dispatches=(DispatchSlot(start=NOW, end=NOW + timedelta(hours=1)),),
                  load_slots=(0.5,) * 12 + (1.0,) * 36)
    raw = site().model_dump()
    raw["major_loads"][0]["power_entity"] = "sensor.ev"
    plan = compute_plan(
        inp, cfg, fixed_schedule(inp, cfg), topology=Topology.model_validate(raw),
        major_loads=(MajorLoadObservation(load_id="ev", reading_kw=0.74, reported_at=NOW),),
    )
    assert plan.required_kwh == pytest.approx(19.5)


def test_reserve_reason_only_when_reserve_causes_shortfall() -> None:
    cfg, inp = config(), replace(inputs(), now=NOW)
    inp = replace(inp, load_slots=(0.5,) * 12 + (0.2,) * 36)
    topology = site()
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.reserve_limit_reason == "Charging limited by the major-load reserve."
    assert plan.hardware_limit_reason is None
    assert plan_for(site(import_a=1)).reserve_limit_reason is None
    assert plan_for(site(import_a=1)).hardware_limit_reason is not None


def test_bank_limited_shortfall_still_names_the_bank_when_reserve_also_narrows() -> None:
    # Bank 1.04 kW DC binds without the reserve; the reserve narrows import to 0.96 kW,
    # but removing it still misses the target, so this is a hardware shortfall.
    plan = plan_for(site(bank_a=20))
    assert plan.reserve_limit_reason is None
    assert plan.hardware_limit_reason == "Charging limited by hardware: legacy_bank."


def test_satisfied_target_has_no_reserve_reason() -> None:
    cfg, inp = config(), inputs()
    inp = replace(inp, load_slots=(0.5,) * 12 + (0.1,) * 36)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=site())
    assert plan.required_kwh == pytest.approx(3.6)
    assert plan.reserve_limit_reason is None
    assert plan.hardware_limit_reason is None


def test_empty_observations_equal_default_and_no_loads_preserve_plan() -> None:
    cfg, inp = config(), inputs()
    topology = native(cfg)
    assert compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology) == compute_plan(
        inp, cfg, fixed_schedule(inp, cfg), topology=topology, major_loads=())


def test_advisory_uses_maximum_window_forecast_and_daily_model_is_unchanged() -> None:
    cfg, inp = config(), inputs()
    inp = replace(inp, load_slots=(1.2,) + (0.1,) * 47)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=site())
    assert plan.reserve_advisory is not None
    daily = replace(inp, load_slots=None, solar_slots=None)
    plan = compute_plan(daily, cfg, fixed_schedule(daily, cfg), topology=site())
    assert plan.required_kwh == pytest.approx(19.5)
    assert plan.reserve_advisory is None
