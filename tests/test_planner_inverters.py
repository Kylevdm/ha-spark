"""Several inverters on one bank, with V2L, at the pure planner boundary (#241)."""

from dataclasses import replace

import pytest

from ha_spark.energy.models import ChargePlan, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import V2L_NEEDED_KWH, compute_plan
from ha_spark.energy.soc_integrity import SocStatus
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.energy.topology import Topology
from tests.test_planner_limits import config, inputs, native

# Two 62.5 A Solis inverters on one 52 V bank: the #241 acceptance case.
# The fill strategy with a near-full battery pins the split without the
# battery's own room masking which ceiling binds.


def two_inverter(
    cfg: PlannerConfig,
    *,
    bank_a: float = 125,
    import_a: float = 75,
    second_a: float = 62.5,
    v2l_a: float | None = None,
) -> Topology:
    raw = native(cfg, bank_a=bank_a, import_a=import_a).model_dump()
    raw["inverters"] = (
        raw["inverters"][0],
        {**raw["inverters"][0], "id": "second_inverter", "charge_limit_a": second_a},
    )
    if v2l_a is not None:
        raw["v2l_chargers"] = ({"id": "car", "bank_id": "legacy_bank", "max_current_a": v2l_a},)
    return Topology.model_validate(raw)


def plan_for(cfg: PlannerConfig, inp: PlannerInputs, topology: Topology) -> ChargePlan:
    return compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)


def at_soc(value: float) -> PlannerInputs:
    return replace(inputs(), soc=replace(inputs().soc, value=value))


def test_241_bank_binding_two_inverters_share_one_bank() -> None:
    cfg = config(strategy="fill")
    plan = plan_for(cfg, inputs(), two_inverter(cfg, bank_a=100))
    assert plan.effective_limits is not None
    # min(100 A bank, 62.5 A + 62.5 A inverters) = 100 A, counted once.
    assert plan.effective_limits.banks[0].charge_a == pytest.approx(100)
    assert plan.required_kwh == pytest.approx(5.2 * 6)
    assert plan.hardware_limit_reason == "Charging limited by hardware: legacy_bank."
    allocations = plan.inverter_allocations
    assert [allocation.inverter_id for allocation in allocations] == [
        "legacy_inverter", "second_inverter",
    ]
    assert [allocation.charge_a for allocation in allocations] == [50, 50]
    assert sum(allocation.charge_a or 0 for allocation in allocations) <= 100


def test_241_connection_binding_names_the_connection() -> None:
    cfg = config(strategy="fill")
    plan = plan_for(cfg, inputs(), two_inverter(cfg, bank_a=125, import_a=20))
    # (20 A x 240 V - 1 kW base load) x 80% efficiency: import binds, not the bank.
    assert plan.required_kwh == pytest.approx(3.04 * 6)
    assert plan.hardware_limit_reason == "Charging limited by hardware: legacy_connection."
    allocations = plan.inverter_allocations
    assert [allocation.charge_a for allocation in allocations] == [36, 36]
    bank_a = min(125, plan.required_kwh / 0.8 / (6 * 0.052))
    assert sum(allocation.charge_a or 0 for allocation in allocations) <= bank_a + 1e-9
    assert all((allocation.charge_a or 0) <= 62.5 for allocation in allocations)


def test_241_bank_and_connection_both_allow_125a() -> None:
    cfg = config(strategy="fill")
    plan = plan_for(cfg, at_soc(55), two_inverter(cfg, bank_a=125, import_a=75))
    assert plan.effective_limits is not None
    assert plan.effective_limits.banks[0].charge_a == 125
    assert plan.hardware_limit_reason is None
    # One bank, one 100 kWh capacity: the room to the cap is counted once.
    assert plan.required_kwh == pytest.approx(35)
    assert plan.target_soc == pytest.approx(90)
    # 125 A split evenly is 62.5 A each, rounded down to whole amps.
    assert [allocation.charge_a for allocation in plan.inverter_allocations] == [62, 62]


def test_uneven_inverter_limits_split_proportionally_within_their_own() -> None:
    cfg = config(strategy="fill")
    plan = plan_for(cfg, at_soc(55), two_inverter(cfg, bank_a=125, second_a=40))
    assert plan.effective_limits is not None
    assert plan.effective_limits.banks[0].charge_a == pytest.approx(102.5)
    allocations = plan.inverter_allocations
    assert [allocation.charge_a for allocation in allocations] == [62, 40]
    assert sum(allocation.charge_a or 0 for allocation in allocations) <= 102.5
    assert all(
        (allocation.charge_a or 0) <= limit
        for allocation, limit in zip(allocations, (62.5, 40), strict=True)
    )


@pytest.mark.parametrize(("peak_kwh", "status", "native_transport", "full_speed"), [
    (31.0, SocStatus.OK, True, True),
    (31.0, SocStatus.UNAVAILABLE, True, False),
    (8.0, SocStatus.OK, True, False),
    (16.0, SocStatus.OK, True, False),  # exactly 1 kWh short
    (31.0, SocStatus.OK, False, False),
])
def test_full_speed_only_for_a_trusted_native_shortfall_over_threshold(
    peak_kwh: float, status: SocStatus, native_transport: bool, full_speed: bool,
) -> None:
    cfg = config(capacity_kwh=20, target_cap=95)
    inp = inputs()
    inp = replace(inp, soc=replace(inp.soc, status=status),
                  load_slots=(0.0,) * 12 + (peak_kwh / 32,) * 32 + (0.0,) * 4,
                  car_plugged_in=True)
    raw = two_inverter(cfg, bank_a=100, v2l_a=30).model_dump()
    if native_transport:
        for i, inverter in enumerate(raw["inverters"]):
            inverter.update(transport="native", gateway_host=f"gateway-{i}.test")
    plan = plan_for(cfg, inp, Topology.model_validate(raw))
    expected_shortfall = max(0.0, peak_kwh - 15)
    assert plan.shortfall_after_window_kwh == pytest.approx(expected_shortfall)
    assert plan.charge_intent.full_speed is full_speed
    spread = plan.required_kwh / cfg.charge_efficiency / (6 * 0.052)
    assert spread < 100
    assert [allocation.charge_a for allocation in plan.inverter_allocations] == [
        50 if full_speed else int(spread / 2),
    ] * 2


def test_legacy_plan_keeps_no_allocations_even_with_a_plugged_car() -> None:
    cfg = config(strategy="fill")
    inp = replace(inputs(), car_plugged_in=True)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg))
    assert plan.inverter_allocations == ()
    # The in-window capacity stays 62.5 A x 52 V.
    assert plan.required_kwh == pytest.approx(3.25 * 6)
    plain = compute_plan(inputs(), config(strategy="fill"), fixed_schedule(inputs(), cfg))
    assert plain.inverter_allocations == ()
    assert not plan.charge_intent.full_speed
    assert plain.required_kwh == plan.required_kwh


def test_fail_closed_plan_keeps_no_allocations() -> None:
    cfg, inp = config(), inputs()
    raw = two_inverter(cfg).model_dump()
    raw["banks"][0]["served_load_share_pct"] = 50
    raw["banks"] = (*raw["banks"], {**raw["banks"][0], "id": "annex_bank"})
    topology = Topology.model_validate(raw)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology)
    assert plan.required_kwh == 0
    assert plan.limits_reason is not None
    assert plan.shortfall_after_window_kwh > V2L_NEEDED_KWH
    assert not plan.charge_intent.full_speed
    assert plan.inverter_allocations == ()
