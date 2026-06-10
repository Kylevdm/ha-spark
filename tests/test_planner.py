"""Tests for the pure charge-planner math."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest

from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan


def cfg(**kw: Any) -> PlannerConfig:
    base: dict[str, Any] = dict(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=1.0,
        window_start=time(23, 30),
        window_end=time(5, 30),
        buffer_pct=0.0,
        charge_efficiency=1.0,
    )
    base.update(kw)
    return PlannerConfig(**base)


def test_window_hours_wraps_midnight() -> None:
    assert cfg().window_hours == 6.0


def test_basic_required_and_current() -> None:
    inp = PlannerInputs(soc_now=30, solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    plan = compute_plan(inp, cfg())
    # deficit 15.45 - usable 2.688 = 12.76 kWh; target 30 + 12.76/26.88*100 ~ 77%
    assert round(plan.required_kwh, 2) == 12.76
    assert 77 <= plan.target_soc <= 78
    # 12.76 / (6h * 51V/1000) ~ 41.7 A
    assert 41 <= plan.overnight_current_a <= 42
    assert plan.actions[0].kind == "set_charge_current"
    assert plan.actions[0].current_a == 42


def test_zero_need_when_full_and_sunny() -> None:
    inp = PlannerInputs(soc_now=90, solar_tomorrow_kwh=30, predicted_home_load_kwh=10)
    plan = compute_plan(inp, cfg())
    assert plan.required_kwh == 0
    assert plan.overnight_current_a == 0


def test_soc_validity_passes_through_to_plan() -> None:
    inp = PlannerInputs(
        soc_now=0, solar_tomorrow_kwh=3, predicted_home_load_kwh=10, soc_valid=False
    )
    assert compute_plan(inp, cfg()).soc_valid is False
    valid = PlannerInputs(soc_now=30, solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    assert compute_plan(valid, cfg()).soc_valid is True


def test_buffer_inflates_required_within_headroom() -> None:
    inp = PlannerInputs(soc_now=20, solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    plan = compute_plan(inp, cfg(buffer_pct=20.0))
    # deficit = 10 - 3 = 7; usable_now = 0 (soc at min); buffered = 7 * 1.2 = 8.4.
    assert plan.deficit_kwh == pytest.approx(7.0)
    assert plan.buffer_pct == pytest.approx(20.0)
    assert plan.required_kwh == pytest.approx(8.4)


def test_headroom_caps_required() -> None:
    inp = PlannerInputs(soc_now=85, solar_tomorrow_kwh=0, predicted_home_load_kwh=50)
    plan = compute_plan(inp, cfg())
    headroom = 26.88 * (90 - 85) / 100  # 1.344
    assert round(plan.required_kwh, 3) == round(headroom, 3)
    assert plan.target_soc <= 90 + 1e-9


def _slot(hh: int, mm: int) -> DispatchSlot:
    start = datetime(2026, 6, 8, hh, mm, tzinfo=UTC)
    return DispatchSlot(start, start + timedelta(minutes=30), -2.0, "SMART")


def test_daytime_dispatch_emits_stop_discharge() -> None:
    inp = PlannerInputs(
        soc_now=30, solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(13, 0),),
    )
    plan = compute_plan(inp, cfg())
    assert any(a.kind == "stop_discharge" for a in plan.actions)
    assert plan.cheap_covered_kwh > 0


def test_overnight_dispatch_does_not_stop_discharge() -> None:
    inp = PlannerInputs(
        soc_now=30, solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2,
        dispatches=(_slot(2, 0),),
    )
    plan = compute_plan(inp, cfg())
    assert not any(a.kind == "stop_discharge" for a in plan.actions)
    assert plan.cheap_covered_kwh == 0


# --- v2 per-slot model ---

# Horizon origin: tonight 23:30 (the window start).
_HORIZON_START = datetime(2026, 6, 8, 23, 30, tzinfo=UTC)


def _slot_inputs(
    load: float = 0.5,
    solar_slots: tuple[float, ...] | None = None,
    dispatches: tuple[DispatchSlot, ...] = (),
    soc_now: float = 20.0,
) -> PlannerInputs:
    load_slots = (load,) * 48
    return PlannerInputs(
        soc_now=soc_now,
        solar_tomorrow_kwh=sum(solar_slots) if solar_slots else 0.0,
        predicted_home_load_kwh=sum(load_slots),
        dispatches=dispatches,
        load_slots=load_slots,
        solar_slots=solar_slots,
        horizon_start=_HORIZON_START,
    )


def test_slot_model_charges_for_expensive_slots_only() -> None:
    # 0.5 kWh per slot; 12 window slots are cheap -> 36 expensive slots = 18 kWh.
    plan = compute_plan(_slot_inputs(load=0.5, soc_now=20.0), cfg())
    assert plan.model == "slots"
    assert plan.expensive_load_kwh == pytest.approx(18.0)
    # SoC at min -> usable 0; required = 18 kWh within headroom (18.816).
    assert plan.required_kwh == pytest.approx(18.0)


def test_slot_model_subtracts_solar_per_slot() -> None:
    # 2 kWh of solar in a 0.5 kWh slot only removes that slot's load — per-slot
    # clamping means excess solar can't offset other slots.
    solar = [0.0] * 48
    solar[24] = 2.0  # 11:30 next day
    solar[26] = 2.0
    plan = compute_plan(_slot_inputs(load=0.5, solar_slots=tuple(solar)), cfg())
    assert plan.expensive_load_kwh == pytest.approx(17.0)  # 18 - 2 * 0.5
    assert plan.required_kwh == pytest.approx(17.0)


def test_slot_model_dispatch_overlap_is_fractional() -> None:
    # A dispatch covering one full expensive slot removes 0.5 kWh from the need
    # and still emits stop_discharge.
    d_start = _HORIZON_START + timedelta(hours=14)  # 13:30 next day
    dispatch = DispatchSlot(d_start, d_start + timedelta(minutes=30), -2.0, "SMART")
    plan = compute_plan(_slot_inputs(load=0.5, dispatches=(dispatch,)), cfg())
    assert plan.expensive_load_kwh == pytest.approx(17.5)
    assert any(a.kind == "stop_discharge" for a in plan.actions)
    assert plan.cheap_covered_kwh == pytest.approx(0.5)


def test_slot_model_costs() -> None:
    plan = compute_plan(_slot_inputs(load=0.5, soc_now=20.0), cfg())
    # Baseline: 6 cheap kWh at 0.069 + 18 expensive kWh at 0.30.
    assert plan.baseline_cost == pytest.approx(6 * 0.069 + 18 * 0.30)
    # Planned: cheap load + the 18 kWh charge all at off-peak, nothing uncovered.
    assert plan.planned_cost == pytest.approx((6 + 18) * 0.069)
    assert plan.planned_cost < plan.baseline_cost


def test_daily_model_also_reports_costs() -> None:
    inp = PlannerInputs(soc_now=30, solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    plan = compute_plan(inp, cfg())
    assert plan.model == "daily"
    assert plan.expensive_load_kwh is None
    assert plan.baseline_cost is not None and plan.planned_cost is not None
    assert plan.planned_cost <= plan.baseline_cost + 1e-9


def test_slot_model_respects_headroom_and_max_current() -> None:
    plan = compute_plan(_slot_inputs(load=2.0, soc_now=85.0), cfg())
    headroom = 26.88 * (90 - 85) / 100
    assert plan.required_kwh == pytest.approx(headroom)
    assert plan.overnight_current_a <= 62.5


def test_fill_strategy_charges_to_cap() -> None:
    inp = PlannerInputs(soc_now=69, solar_tomorrow_kwh=3.4, predicted_home_load_kwh=17.7)
    plan = compute_plan(inp, cfg(strategy="fill"))
    headroom = 26.88 * (90 - 69) / 100
    assert plan.required_kwh == pytest.approx(headroom)
    assert plan.target_soc == pytest.approx(90.0)
    assert plan.strategy == "fill"
    # Fill ignores need entirely: it buys more than the deficit strategy would.
    assert plan.required_kwh > compute_plan(inp, cfg()).required_kwh


def test_fill_strategy_zero_at_cap() -> None:
    inp = PlannerInputs(soc_now=90, solar_tomorrow_kwh=3.4, predicted_home_load_kwh=17.7)
    plan = compute_plan(inp, cfg(strategy="fill"))
    assert plan.required_kwh == 0.0
    assert plan.overnight_current_a == 0.0


def test_pre_window_drain_reduces_usable() -> None:
    base = PlannerInputs(soc_now=30, solar_tomorrow_kwh=8.75, predicted_home_load_kwh=24.2)
    drained = PlannerInputs(
        soc_now=30,
        solar_tomorrow_kwh=8.75,
        predicted_home_load_kwh=24.2,
        pre_window_drain_kwh=1.0,
    )
    plan_base = compute_plan(base, cfg())
    plan_drained = compute_plan(drained, cfg())
    # The kWh drained before the window must be bought back on top.
    assert plan_drained.required_kwh == pytest.approx(plan_base.required_kwh + 1.0)
    assert plan_drained.pre_window_drain_kwh == 1.0
    # Raw usable_now is reported unchanged; only sizing uses the window value.
    assert plan_drained.usable_now_kwh == pytest.approx(plan_base.usable_now_kwh)


def test_charge_efficiency_inflates_purchase_and_current() -> None:
    inp = PlannerInputs(soc_now=20, solar_tomorrow_kwh=3, predicted_home_load_kwh=10)
    lossless = compute_plan(inp, cfg())
    lossy = compute_plan(inp, cfg(charge_efficiency=0.9))
    # Stored energy target is unchanged; the AC purchase and current grow by 1/0.9.
    assert lossy.required_kwh == pytest.approx(lossless.required_kwh)
    assert lossy.overnight_current_a == pytest.approx(lossless.overnight_current_a / 0.9)
    assert lossy.charge_efficiency == pytest.approx(0.9)
    assert lossless.planned_cost is not None and lossy.planned_cost is not None
    extra_buy = lossless.required_kwh / 0.9 - lossless.required_kwh
    assert lossy.planned_cost == pytest.approx(lossless.planned_cost + extra_buy * 0.069)


def test_export_revenue_adjusts_both_costs_equally() -> None:
    # Daily model: 10 kWh solar vs 6 kWh load -> 4 kWh exported.
    inp = PlannerInputs(soc_now=50, solar_tomorrow_kwh=10, predicted_home_load_kwh=6)
    without = compute_plan(inp, cfg())
    with_export = compute_plan(inp, cfg(rate_export=0.15))
    assert without.export_revenue is None
    assert with_export.export_revenue == pytest.approx(4 * 0.15)
    assert without.baseline_cost is not None and with_export.baseline_cost is not None
    assert without.planned_cost is not None and with_export.planned_cost is not None
    assert with_export.baseline_cost == pytest.approx(without.baseline_cost - 0.6)
    assert with_export.planned_cost == pytest.approx(without.planned_cost - 0.6)
    # The decision (required charge) is unchanged.
    assert with_export.required_kwh == pytest.approx(without.required_kwh)


def test_slot_model_export_revenue_sums_per_slot_surplus() -> None:
    solar = [0.0] * 48
    solar[24] = 2.0  # 0.5 kWh load slots -> 1.5 kWh surplus in this slot
    plan = compute_plan(
        _slot_inputs(load=0.5, solar_slots=tuple(solar)), cfg(rate_export=0.10)
    )
    assert plan.export_revenue == pytest.approx(1.5 * 0.10)
