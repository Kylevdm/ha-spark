"""Tests for the pure charge-planner math."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

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
