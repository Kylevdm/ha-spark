"""Import-price overlay precedence and daytime-purchase winners (#695).

Order, highest first: holds, Axle export events, negative explicit prices,
Free Electricity, Happy Hour. One effective price per slot, never stacked.
"""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ha_spark.energy.models import DispatchSlot, FlexibilityEvent, FreeSession
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import apply_import_overlay, fixed_schedule
from tests.test_planner import cfg
from tests.test_planner_daytime import _dynamic_inputs, _dynamic_schedule
from tests.test_planner_slots_view import LONDON, slot_inputs

NOW = datetime(2026, 1, 12, 12, tzinfo=LONDON)
FREE = FreeSession("free_electricity", "free", NOW, NOW + timedelta(hours=1))
HAPPY = FreeSession("happy_hour", "happy", NOW, NOW + timedelta(hours=1))


def _event(start: datetime, end: datetime) -> FlexibilityEvent:
    return FlexibilityEvent(start, end, "export", NOW, 1.0)


def _zero_load(inputs):
    n = len(inputs.load_slots)
    return replace(inputs, load_slots=(0.0,) * n, solar_slots=(0.0,) * n)


def _planned(inputs, config, dynamic=False, **kwargs):
    schedule = _dynamic_schedule(inputs, config) if dynamic else fixed_schedule(inputs, config)
    return compute_plan(inputs, config, apply_import_overlay(schedule, inputs, config),
                        **kwargs)


def _slot_at(geometry, moment: datetime) -> int:
    return geometry.slot_starts.index(moment.astimezone(geometry.horizon_start.tzinfo))


# The adjacent pairs around Power-up are reserved for #684 and cannot be
# exercised yet; the implemented pairs above and below are covered here.


def test_hold_outranks_axle_event_and_negative_price_in_one_slot():
    config = cfg(capacity_kwh=100, strategy="fill")
    hold = (NOW + timedelta(minutes=30), NOW + timedelta(hours=1))
    inputs = _dynamic_inputs(NOW, config, [-0.05] + [0.30] * 70)
    inputs = replace(
        inputs,
        dispatches=(DispatchSlot(*hold),),
        flexibility_event=_event(*hold),
        free_sessions=(FREE,),
    )
    schedule = apply_import_overlay(_dynamic_schedule(inputs, config), inputs, config)
    i = _slot_at(inputs.geometry, hold[0])
    assert schedule.import_overlays[i] is None
    assert schedule.suppressed_import_sessions[i] == FREE
    assert schedule.import_suppression_reasons[i] == "EV or dispatch hold"
    # The hold wins: no daytime purchase in that slot, not even at a negative price.
    plan = compute_plan(inputs, config, schedule)
    assert all(not (p.slot_range[0] <= i < p.slot_range[1]) for p in plan.daytime_purchases)


def test_axle_export_event_outranks_a_negative_price():
    config = cfg(capacity_kwh=100, strategy="fill")
    inputs = _dynamic_inputs(NOW, config, [-0.05] + [0.30] * 70)
    inputs = replace(inputs, flexibility_event=_event(NOW, NOW + timedelta(minutes=30)))
    plan = _planned(inputs, config, dynamic=True)
    assert plan.daytime_purchases == ()


def test_negative_price_beats_free_session_and_names_the_loser():
    config = cfg(capacity_kwh=100, strategy="fill")
    inputs = _zero_load(_dynamic_inputs(NOW, config, [-0.05] + [0.30] * 70))
    inputs = replace(inputs, free_sessions=(FREE,))
    schedule = apply_import_overlay(_dynamic_schedule(inputs, config), inputs, config)
    assert schedule.import_sessions[0] is None
    assert schedule.import_overlay_losers[0] == "free_electricity"
    assert schedule.import_allowances[0] is None
    assert schedule.import_overlays[1] == "free_electricity"
    plan = compute_plan(inputs, config, schedule)
    first, second = plan.daytime_purchases
    assert first.overlay == "dynamic_price"
    assert first.effective_price == pytest.approx(-0.05)
    assert "negative explicit tariff price" in first.reason
    assert "Free Electricity was set aside" in first.reason
    assert second.overlay == "free_electricity"
    assert second.effective_price == pytest.approx(0.0)


@pytest.mark.parametrize("daytime_price", [-0.05, -0.10])
def test_negative_price_must_still_beat_the_selected_overnight_average(daytime_price):
    config = cfg(strategy="fill", buffer_pct=0)
    inputs = _dynamic_inputs(NOW, config, [daytime_price] + [0.30] * 22 + [-0.10] * 48)
    inputs = replace(inputs, free_sessions=(replace(FREE, end=NOW + timedelta(minutes=30)),))
    schedule = apply_import_overlay(_dynamic_schedule(inputs, config), inputs, config)
    assert schedule.overnight_average_price == pytest.approx(-0.10)
    assert schedule.import_sessions[0] is None
    ordinary = compute_plan(
        inputs, config, replace(schedule, explicit_import_prices=()),
    )
    plan = compute_plan(inputs, config, schedule)
    assert plan.daytime_purchases == ()
    assert plan.charge_intent.daytime_windows == ()
    assert plan.required_kwh == ordinary.required_kwh
    assert plan.planned_cost == ordinary.planned_cost


def test_negative_price_slot_does_not_spend_the_happy_hour_allowance():
    config = cfg(capacity_kwh=100, strategy="fill", charge_efficiency=1.0)
    inputs = _zero_load(_dynamic_inputs(NOW, config, [-0.05] + [0.30] * 70))
    inputs = replace(inputs, free_sessions=(HAPPY,), happy_hour_remaining_kwh={"happy": 16.0})
    schedule = apply_import_overlay(_dynamic_schedule(inputs, config), inputs, config)
    assert schedule.import_sessions[0] is None
    assert schedule.import_allowances[0] is None
    assert schedule.import_overlay_losers[0] == "happy_hour"
    assert schedule.import_overlays[1] == "happy_hour"
    assert schedule.import_allowances[1] == pytest.approx(14.4)
    plan = compute_plan(inputs, config, schedule)
    assert [p.overlay for p in plan.daytime_purchases] == ["dynamic_price", "happy_hour"]
    assert plan.daytime_purchases[0].session_id == ""
    # The Happy Hour run keeps its own allowance; only its own buy is counted.
    happy = plan.daytime_purchases[1]
    assert happy.session_id == "happy"
    assert happy.effective_price == pytest.approx(0.0)
    assert happy.energy_kwh > 0


def test_free_electricity_wins_a_shared_slot_over_happy_hour():
    config = cfg(capacity_kwh=100, strategy="fill")
    inputs = _zero_load(replace(
        slot_inputs(NOW, config), free_sessions=(HAPPY, FREE),
        happy_hour_remaining_kwh={"happy": 16.0},
    ))
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert schedule.import_overlays[:2] == ("free_electricity", "free_electricity")
    assert schedule.import_overlay_losers[:2] == ("happy_hour", "happy_hour")
    purchase, = compute_plan(inputs, config, schedule).daytime_purchases
    assert purchase.overlay == "free_electricity"
    assert "Free Electricity" in purchase.reason
    assert "Happy Hour set aside" in purchase.reason


def test_session_partly_overlapping_an_axle_event_buys_only_outside_it():
    config = cfg(capacity_kwh=100, strategy="fill")
    inputs = _zero_load(replace(
        slot_inputs(NOW, config),
        free_sessions=(FREE,),
        flexibility_event=_event(NOW + timedelta(minutes=30), NOW + timedelta(hours=1)),
    ))
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert schedule.import_overlays[:2] == ("free_electricity", None)
    assert schedule.suppressed_import_sessions[1] == FREE
    assert schedule.import_suppression_reasons[1] == "Axle export event"
    purchase, = compute_plan(inputs, config, schedule).daytime_purchases
    assert purchase.slot_range == (0, 1)
    assert purchase.start == NOW.astimezone(inputs.geometry.slot_starts[0].tzinfo)
    assert purchase.end == NOW.astimezone(inputs.geometry.slot_starts[0].tzinfo) \
        + timedelta(minutes=30)


def test_session_partly_under_a_dispatch_hold_buys_only_outside_it():
    config = cfg(capacity_kwh=100, strategy="fill")
    hold = (NOW + timedelta(minutes=30), NOW + timedelta(hours=1))
    inputs = _zero_load(replace(
        slot_inputs(NOW, config),
        free_sessions=(FREE,),
        dispatches=(DispatchSlot(*hold),),
    ))
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert schedule.import_overlays[:2] == ("free_electricity", None)
    assert schedule.suppressed_import_sessions[1] == FREE
    assert schedule.import_suppression_reasons[1] == "EV or dispatch hold"
    plan = compute_plan(inputs, config, schedule)
    purchase, = plan.daytime_purchases
    assert purchase.slot_range == (0, 1)
    assert plan.charge_intent.holds == (hold,)


def test_current_ev_hold_suppresses_the_current_slot_only():
    config = cfg(capacity_kwh=100, strategy="fill")
    inputs = _zero_load(replace(
        slot_inputs(NOW, config),
        free_sessions=(FREE,),
        ev_hold_charging=True,
    ))
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert schedule.import_overlays[0] is None
    assert schedule.suppressed_import_sessions[0] == FREE
    assert schedule.import_overlays[1] == "free_electricity"
    purchase, = compute_plan(inputs, config, schedule).daytime_purchases
    assert purchase.slot_range == (1, 2)


@pytest.mark.parametrize("case", ["free", "free_and_happy", "negative", "hold"])
def test_effective_price_is_always_one_offer_never_a_combination(case):
    config = cfg(capacity_kwh=100, strategy="fill")
    prices = [-0.05] + [0.30] * 70 if case == "negative" else [0.10] + [0.30] * 70
    inputs = _zero_load(_dynamic_inputs(NOW, config, prices))
    sessions: tuple[FreeSession, ...] = ()
    if case in ("free", "hold"):
        sessions = (FREE,)
    elif case == "free_and_happy":
        sessions = (HAPPY, FREE)
    inputs = replace(inputs, free_sessions=sessions)
    if case == "hold":
        inputs = replace(inputs, dispatches=(
            DispatchSlot(NOW, NOW + timedelta(minutes=30)),
        ))
    schedule = apply_import_overlay(_dynamic_schedule(inputs, config), inputs, config)
    for i, price in enumerate(schedule.prices):
        effective = (schedule.battery_import_prices[i]
                     if i < len(schedule.battery_import_prices) else price)
        if i < len(schedule.import_overlays) and schedule.import_overlays[i] is not None:
            assert effective == 0.0
        else:
            assert effective == price
        # Exactly one offer is named per slot: never two stacked labels.
        overlay = schedule.import_overlays[i] if i < len(schedule.import_overlays) else None
        assert overlay in (None, "free_electricity", "happy_hour")
