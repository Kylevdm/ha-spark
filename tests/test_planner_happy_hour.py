"""Happy Hour allowance sizing at the pure schedule and planner seams."""
from dataclasses import replace
from datetime import datetime, time, timedelta

import pytest

from ha_spark.energy.models import FreeSession
from ha_spark.energy.tariff import apply_import_overlay, fixed_schedule
from tests.test_planner import _soc, _soc_unavailable, cfg
from tests.test_planner_daytime import planned
from tests.test_planner_limits import native
from tests.test_planner_slots_view import LONDON, slot_inputs


def hour_inputs(config, *, load=0.0, hours=1, now=None):
    now = now or datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    n = len(inputs.load_slots)
    return replace(inputs, load_slots=(load,) * (2 * hours) + (0.0,) * (n - 2 * hours),
                   solar_slots=(0.0,) * n, free_sessions=(
                       FreeSession("happy_hour", "private-id", now,
                                   now + timedelta(hours=hours)),),
                   # #694: absent allowance now fails closed, so fixtures that
                   # relied on the old implicit 16 kWh state it explicitly.
                   happy_hour_remaining_kwh={"private-id": 16.0})


def test_happy_hour_fills_cap_and_shrinks_next_overnight():
    config = cfg(strategy="fill", buffer_pct=0, capacity_kwh=4)
    inputs = hour_inputs(config)
    ordinary = planned(replace(inputs, free_sessions=()), config)
    plan = planned(inputs, config)
    purchase, = plan.daytime_purchases
    assert purchase.energy_kwh == pytest.approx(ordinary.required_kwh)
    assert plan.required_kwh == pytest.approx(0)
    assert plan.charge_intent.daytime_windows[0].target_soc_pct == config.target_cap
    assert purchase.evidence == "booked hour 12:00–13:00"
    assert "private-id" not in purchase.reason
    assert "Happy Hour" in purchase.reason
    assert "headroom to the target cap" in purchase.reason


def test_high_gross_house_load_limits_purchase_even_with_solar():
    config = cfg(capacity_kwh=100, charge_efficiency=0.8)
    inputs = hour_inputs(config, load=6)
    inputs = replace(inputs, solar_slots=(4.0,) * len(inputs.load_slots))
    purchase, = planned(inputs, config).daytime_purchases
    assert purchase.energy_kwh == pytest.approx(2.4 * config.charge_efficiency)
    assert 12 + purchase.energy_kwh / config.charge_efficiency <= 14.4 + 1e-9
    assert "the Happy Hour allowance" in purchase.reason


def test_adjacent_hours_never_pool_unused_allowance():
    config = cfg(capacity_kwh=100, max_current_a=50)
    inputs = hour_inputs(config, hours=2)
    first = replace(inputs.free_sessions[0], end=inputs.now + timedelta(hours=1))
    second = replace(first, session_id="second", start=first.end,
                     end=first.end + timedelta(hours=1))
    inputs = replace(inputs, free_sessions=(first, second),
                     happy_hour_remaining_kwh={"private-id": 16.0, "second": 3.6})
    purchases = planned(inputs, config).daytime_purchases
    assert len(purchases) == 2
    assert purchases[0].energy_kwh == pytest.approx(config.voltage_v * 50 / 1000)
    assert purchases[1].energy_kwh == pytest.approx(2 * config.charge_efficiency)
    assert "the charge rate" in purchases[0].reason
    assert "13:00–14:00" in purchases[1].reason


@pytest.mark.parametrize("remaining", [0, -1, float("nan"), float("inf"), True, "4", None])
def test_invalid_or_zero_remaining_leaves_ordinary_schedule_and_plan(remaining):
    config = cfg()
    inputs = replace(hour_inputs(config), happy_hour_remaining_kwh={"private-id": remaining})
    schedule = fixed_schedule(inputs, config)
    assert apply_import_overlay(schedule, inputs, config) is schedule
    assert planned(inputs, config) == planned(replace(inputs, free_sessions=()), config)


def test_remaining_above_sixteen_clamps_and_long_session_has_one_allowance():
    config = cfg(capacity_kwh=100, charge_efficiency=0.8)
    inputs = replace(hour_inputs(config, hours=6),
                     happy_hour_remaining_kwh={"private-id": 40})
    purchase, = planned(inputs, config).daytime_purchases
    assert purchase.energy_kwh == pytest.approx(14.4 * config.charge_efficiency)


@pytest.mark.parametrize("failed_soc", [False, True])
def test_untrusted_soc_or_limits_never_buys(failed_soc):
    config = cfg()
    inputs = hour_inputs(config)
    if failed_soc:
        inputs = replace(inputs, soc=_soc_unavailable())
    plan = planned(inputs, config, **({} if failed_soc else {"limits_unavailable": "unavailable"}))
    assert plan.daytime_purchases == ()


def test_post_window_hour_leaves_overnight_sizing_and_reservations_identical():
    config = cfg()
    inputs = slot_inputs(datetime(2026, 1, 12, 20, tzinfo=LONDON), config)
    ordinary = planned(inputs, config, topology=native(config))
    start = datetime(2026, 1, 13, 12, tzinfo=LONDON)
    inputs = replace(inputs, free_sessions=(
        FreeSession("happy_hour", "x", start, start + timedelta(hours=1)),),
        happy_hour_remaining_kwh={"x": 16.0})
    plan = planned(inputs, config, topology=native(config))
    assert plan.daytime_purchases
    for name in ("required_kwh", "target_soc", "inverter_allocations", "reservations",
                 "pre_window_drain_kwh", "charge_funded_reservations"):
        assert getattr(plan, name) == getattr(ordinary, name)
    assert replace(plan.charge_intent, daytime_windows=()) == ordinary.charge_intent


def test_house_load_is_free_only_when_happy_hour_buys():
    config = cfg(strategy="fill", buffer_pct=0)
    inputs = hour_inputs(config, load=0.5)
    plan = planned(inputs, config)
    assert plan.planned_cost == pytest.approx(
        plan.required_kwh / config.charge_efficiency * config.rate_offpeak)
    # With no headroom and no purchase the overlay changes neither discharge nor cost.
    inputs = replace(inputs, soc=_soc(config.target_cap))
    assert planned(inputs, config) == planned(replace(inputs, free_sessions=()), config)


def test_free_electricity_beside_happy_hour_is_unchanged():
    config = cfg(capacity_kwh=100)
    inputs = hour_inputs(config)
    happy = inputs.free_sessions[0]
    free = replace(happy, kind="free_electricity", session_id="free", start=happy.end,
                   end=happy.end + timedelta(hours=1))
    ordinary = planned(replace(inputs, free_sessions=(free,)), config)
    mixed = planned(replace(inputs, free_sessions=(happy, free)), config)
    assert mixed.daytime_purchases[1] == ordinary.daytime_purchases[0]


def test_split_runs_of_one_hour_share_one_allowance():
    config = cfg(capacity_kwh=100, charge_efficiency=0.8)
    inputs = hour_inputs(config, hours=2)
    happy = inputs.free_sessions[0]
    free = replace(happy, kind="free_electricity", session_id="free",
                   start=happy.start + timedelta(minutes=30),
                   end=happy.start + timedelta(hours=1))
    inputs = replace(inputs, free_sessions=(free, happy),
                     happy_hour_remaining_kwh={"private-id": 4.6})
    plan = planned(inputs, config)
    purchases = [p for p in plan.daytime_purchases if p.overlay == "happy_hour"]
    assert len(purchases) == 2
    assert sum(p.energy_kwh for p in purchases) == pytest.approx(3 * config.charge_efficiency)
    assert all("the Happy Hour allowance" in p.reason or "the charge rate" in p.reason
               for p in purchases)


def test_hour_straddling_overnight_buys_only_before_window():
    config = cfg(capacity_kwh=100, window_start=time(12, 30), window_end=time(13))
    inputs = hour_inputs(config, hours=2)
    plan = planned(inputs, config)
    purchase, = plan.daytime_purchases
    assert purchase.start == inputs.now
    assert purchase.end == inputs.now + timedelta(minutes=30)


def test_unpurchased_hour_changes_nothing_beside_another_purchase():
    config = cfg()
    inputs = hour_inputs(config, load=0.5)
    happy = inputs.free_sessions[0]
    free = replace(happy, kind="free_electricity", session_id="free",
                   start=happy.end + timedelta(hours=2),
                   end=happy.end + timedelta(hours=3))
    inputs = replace(inputs, soc=_soc(config.target_cap))
    ordinary = planned(replace(inputs, free_sessions=(free,)), config)
    mixed = planned(replace(inputs, free_sessions=(happy, free)), config)
    assert ordinary.daytime_purchases
    assert mixed == ordinary
