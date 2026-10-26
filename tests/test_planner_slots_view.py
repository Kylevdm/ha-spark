"""Slot view reads the planner's numbers and decisions without changing intent."""

from dataclasses import replace
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.horizon import SLOT, compute_horizon_geometry
from ha_spark.energy.models import DispatchSlot, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import fixed_schedule
from tests.test_planner import _reserve_trade_plan, _soc, _soc_unavailable, cfg

LONDON = ZoneInfo("Europe/London")


def slot_inputs(now, config, **kwargs):
    geometry = compute_horizon_geometry(now, config.window_start, config.window_end, LONDON)
    n = len(geometry.slot_starts)
    return PlannerInputs(
        soc=_soc(30), solar_tomorrow_kwh=4, predicted_home_load_kwh=n * 0.6,
        load_slots=(0.6,) * n, solar_slots=(0.2,) * n,
        geometry=geometry, horizon_start=geometry.horizon_start, now=now, **kwargs,
    )


@pytest.mark.parametrize("now", [
    datetime(2026, 1, 11, 23, 10, tzinfo=LONDON),  # part elapsed, before window
    datetime(2026, 1, 12, 2, 17, tzinfo=LONDON),  # in-window replan
    datetime(2026, 1, 12, 5, 20, tzinfo=LONDON),  # window ended within slot 0
    datetime(2026, 3, 28, 23, 10, tzinfo=LONDON),  # spring forward
    datetime(2026, 10, 24, 23, 10, tzinfo=LONDON),  # fall back
])
def test_slot_view_charge_uses_remaining_window_overlap(now):
    config = cfg(window_start=time(23, 45), window_end=time(5, 15), strategy="fill")
    inputs = slot_inputs(now, config)
    schedule = fixed_schedule(inputs, config)
    plan = compute_plan(inputs, config, schedule)
    assert plan.required_kwh > 0
    assert plan.view is not None
    assert plan.view.soc == ()
    assert len(plan.view.slots) == len(inputs.geometry.slot_starts)
    for i, slot in enumerate(plan.view.slots):
        start = inputs.geometry.slot_starts[i]
        lo = max(start, now.astimezone(UTC))
        overlap = max(lo, inputs.geometry.window_start) < min(
            start + SLOT, inputs.geometry.window_end,
        )
        assert ("charge" in slot.actions) == overlap
        assert slot.start == start
        assert slot.import_price == schedule.prices[i]
        assert slot.export_price == (schedule.export_prices[i] if schedule.export_prices
                                     else schedule.export_rate)
        assert slot.cheap_frac == schedule.cheap_fracs[i]
        remaining = inputs.geometry.slot0_remaining_frac if i == 0 else 1
        assert slot.load_kwh == pytest.approx(0.6 * remaining)
        assert slot.solar_kwh == pytest.approx(0.2 * remaining)


def test_slot_view_no_purchase_even_when_every_slot_is_cheap():
    config = cfg()
    inputs = replace(slot_inputs(datetime(2026, 1, 12, 2, 17, tzinfo=LONDON), config),
                     soc=_soc(90))
    # Keep the input length tied to geometry, rather than a clock-slot count.
    inputs = replace(inputs, load_slots=(0.0,) * len(inputs.geometry.slot_starts))
    schedule = fixed_schedule(inputs, config)
    schedule = replace(schedule, cheap_fracs=(1.0,) * len(schedule.prices))
    plan = compute_plan(inputs, config, schedule)
    assert plan.required_kwh == 0
    assert all("charge" not in slot.actions for slot in plan.view.slots)


@pytest.mark.parametrize("trusted", [True, False])
def test_slot_view_holds_mid_slot_and_ignores_elapsed_evidence(trusted):
    config = cfg(solar_haircut_k=0.5)
    now = datetime(2026, 1, 12, 12, 17, tzinfo=LONDON)
    inputs = slot_inputs(now, config, dispatches=(
        DispatchSlot(now.replace(minute=0), now.replace(minute=10)),
        DispatchSlot(now.replace(minute=25), now.replace(hour=13, minute=0)),
    ))
    if not trusted:
        inputs = replace(inputs, soc=_soc_unavailable())
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    slots = plan.view.slots
    assert "hold" in slots[0].actions
    assert "hold" in slots[1].actions
    assert "hold" not in slots[2].actions  # half-open end
    assert slots[0].solar_kwh == pytest.approx(0.2 * 0.5 * 13 / 30)
    if not trusted:
        assert all(set(slot.actions) <= {"hold"} for slot in slots)
    elapsed_only = replace(inputs, dispatches=inputs.dispatches[:1])
    plan = compute_plan(elapsed_only, config, fixed_schedule(elapsed_only, config))
    assert "hold" not in plan.view.slots[0].actions


def test_slot_view_export_skip_and_v2l_match_existing_decisions():
    # The planner selects two paid-export slots here (tests/test_planner.py).
    plan = _reserve_trade_plan(rate=1.0)
    assert plan.charge_intent.export is not None
    selected = plan.charge_intent.export.selected_slots
    assert selected
    for i, slot in enumerate(plan.view.slots):
        assert ("export" in slot.actions) == (slot.start in selected)
        assert ("skip" in slot.actions) == any(s.start == slot.start for s in plan.export_skips)
        assert ("v2l" in slot.actions) == any(r.slot == i for r in plan.v2l_refills)
        # Without an export-price overlay the planner prices paid export at the event rate.
        if slot.start in selected:
            assert slot.export_price == 1.0


def test_slot_view_v2l_refills_are_not_inferred_from_cheap_slots():
    config = cfg(v2l_budget_kwh=15)
    inputs = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
    )
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    assert plan.v2l_refills
    assert {i for i, slot in enumerate(plan.view.slots) if "v2l" in slot.actions} == {
        refill.slot for refill in plan.v2l_refills
    }


def test_slot_view_reads_naive_hold_bounds_on_the_household_clock():
    config = cfg()
    now = datetime(2026, 6, 8, 12, 17, tzinfo=LONDON)
    inputs = slot_inputs(now, config, dispatches=(
        DispatchSlot(datetime(2026, 6, 8, 12, 30), datetime(2026, 6, 8, 13, 0)),
    ))
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    held = [slot.start for slot in plan.view.slots if "hold" in slot.actions]
    assert held == [datetime(2026, 6, 8, 11, 30, tzinfo=UTC)]
