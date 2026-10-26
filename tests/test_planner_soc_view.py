"""The view projects charging without changing the post-window control ledger."""

from dataclasses import replace
from datetime import UTC, datetime, time

import pytest

from ha_spark.energy.models import DispatchSlot, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import fixed_schedule
from tests.test_planner import (
    _SAME_DAY_EVENT,
    _current_event_inputs,
    _same_day_cfg,
    _same_day_inputs,
    _soc,
    _soc_unavailable,
    cfg,
)
from tests.test_planner_slots_view import LONDON, slot_inputs


@pytest.mark.parametrize("now", [
    datetime(2026, 1, 11, 23, 10, tzinfo=LONDON),
    datetime(2026, 1, 12, 2, 17, tzinfo=LONDON),
    datetime(2026, 3, 28, 23, 10, tzinfo=LONDON),
    datetime(2026, 10, 24, 23, 10, tzinfo=LONDON),
])
def test_soc_forecast_points_and_even_remaining_charge(now):
    config = cfg(window_start=time(23, 45), window_end=time(5, 15), strategy="fill",
                 charge_efficiency=0.8)
    inputs = slot_inputs(now, config)
    geometry = inputs.geometry
    schedule = fixed_schedule(inputs, config)
    plan = compute_plan(inputs, config, schedule)
    points = plan.view.soc
    actual = {p.at: p.soc_pct for p in points}
    clock = now.astimezone(UTC)
    expected = {clock, *(at for at in geometry.slot_starts if at > clock),
                *(at for at in (geometry.window_start, geometry.window_end) if at > clock)}
    assert [p.at for p in points] == sorted(expected)
    assert points[0].soc_pct == inputs.soc_now
    charge_start = max(clock, geometry.window_start)
    # Only the remaining expensive load before the window drains the battery.
    before_kwh = max(0, (charge_start - clock).total_seconds()) / 1800 * 0.4
    start_soc = max(config.min_soc, inputs.soc_now - before_kwh / config.capacity_kwh * 100)
    for point in points:
        if point.at <= charge_start:
            drain = (point.at - clock).total_seconds() / 1800 * 0.4
            assert point.soc_pct == pytest.approx(max(
                config.min_soc, inputs.soc_now - drain / config.capacity_kwh * 100,
            ))
        elif point.at < geometry.window_end:
            fraction = ((point.at - charge_start) / (geometry.window_end - charge_start))
            assert point.soc_pct == pytest.approx(min(
                config.target_cap, start_soc + plan.required_kwh * fraction
                / config.capacity_kwh * 100,
            ))
    for at, soc in plan.soc_path:
        if at > clock:
            assert actual[at] == soc
    assert actual[geometry.window_end] == pytest.approx(min(
        config.target_cap, start_soc + plan.required_kwh / config.capacity_kwh * 100,
    ))


def test_soc_forecast_floor_no_purchase_and_untrusted_daily():
    config = cfg()
    inputs = slot_inputs(datetime(2026, 1, 11, 22, 10, tzinfo=LONDON), config)
    inputs = replace(inputs, soc=_soc(21))
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    assert plan.view.soc[0].soc_pct == 21
    assert min(p.soc_pct for p in plan.view.soc) >= config.min_soc
    unavailable = replace(inputs, soc=_soc_unavailable())
    assert compute_plan(unavailable, config, fixed_schedule(unavailable, config)).view.soc == ()
    daily = replace(inputs, load_slots=None, solar_slots=None, geometry=None)
    assert compute_plan(daily, config, fixed_schedule(daily, config)).view.soc is None
    empty = replace(inputs, soc=_soc(90), load_slots=(0.0,) * len(inputs.load_slots),
                    solar_slots=(0.0,) * len(inputs.load_slots))
    plan = compute_plan(empty, config, fixed_schedule(empty, config))
    assert plan.required_kwh == 0
    assert plan.view.soc
    assert all(p.soc_pct == 90 for p in plan.view.soc)


def test_soc_forecast_pre_window_dispatch_coverage():
    config = cfg(window_start=time(23, 45))
    now = datetime(2026, 1, 11, 22, 10, tzinfo=LONDON)
    inputs = slot_inputs(now, config, dispatches=(DispatchSlot(
        now.replace(minute=30), now.replace(hour=23, minute=0),
    ),))
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    values = {p.at: p.soc_pct for p in plan.view.soc}
    at_2230 = now.replace(minute=30).astimezone(UTC)
    at_2300 = now.replace(hour=23, minute=0).astimezone(UTC)
    assert values[at_2230] == pytest.approx(30 - 0.4 * 20 / 30 / config.capacity_kwh * 100)
    assert values[at_2300] == values[at_2230]


def test_soc_forecast_export_drain_and_empty_post_window_path():
    config = _same_day_cfg()
    now = datetime(2026, 6, 8, 22, 30, tzinfo=UTC)
    event = replace(_SAME_DAY_EVENT, start=now.replace(hour=23),
                    end=datetime(2026, 6, 9, tzinfo=UTC))
    inputs = _current_event_inputs(_same_day_inputs(soc_now=60, now=now, event=event), config)
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    assert plan.charge_intent.export is not None
    assert plan.soc_path == ()
    assert plan.required_kwh == 0
    assert plan.view.soc[-1].at == inputs.geometry.window_end
    values = {p.at: p.soc_pct for p in plan.view.soc}
    # Before the window, both forecast house load and selected export drain it.
    end = inputs.geometry.window_start
    expected = max(config.min_soc, inputs.soc_now
                   - plan.pre_window_drain_kwh / config.capacity_kwh * 100)
    assert values[end] == pytest.approx(expected)
    assert all(p.at <= end or p.soc_pct <= values[end] for p in plan.view.soc)


def test_soc_forecast_post_window_matches_v2l_path():
    config = cfg(v2l_budget_kwh=15)
    inputs = PlannerInputs(
        soc=_soc(20), solar_tomorrow_kwh=0, predicted_home_load_kwh=30,
        load_slots=(0.0,) * 12 + (30 / 36,) * 36, car_plugged_in=True,
        horizon_start=datetime(2026, 1, 10, 23, 30, tzinfo=UTC),
    )
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    assert plan.v2l_refills
    values = {p.at: p.soc_pct for p in plan.view.soc}
    assert all(values[at] == soc for at, soc in plan.soc_path)


def test_soc_forecast_window_end_mid_slot_without_soc_path():
    # With an empty soc_path (export overlapping the charge), the series ends at
    # the window end: the uncovered load after 05:15 in that slot is not yet due.
    from ha_spark.energy.planner import _soc_series

    config = cfg(window_start=time(23, 45), window_end=time(5, 15), strategy="fill")
    inputs = slot_inputs(datetime(2026, 1, 11, 23, 10, tzinfo=LONDON), config)
    schedule = fixed_schedule(inputs, config)
    plan = replace(compute_plan(inputs, config, schedule), soc_path=())
    geometry = inputs.geometry
    net = [max(0.0, load - solar * config.solar_haircut_k)
           for load, solar in zip(inputs.load_slots, inputs.solar_slots, strict=True)]
    net[0] *= geometry.slot0_remaining_frac
    points = _soc_series(plan, inputs, config, schedule, net)
    assert points[-1].at == geometry.window_end
    clock = inputs.now.astimezone(UTC)
    before_kwh = (geometry.window_start - clock).total_seconds() / 1800 * 0.4
    start_soc = max(config.min_soc, inputs.soc_now - before_kwh / config.capacity_kwh * 100)
    assert points[-1].soc_pct == pytest.approx(min(
        config.target_cap, start_soc + plan.required_kwh / config.capacity_kwh * 100,
    ))


def test_soc_forecast_counts_a_legacy_pre_window_drain():
    config = cfg(window_start=time(23, 45), window_end=time(5, 15))
    inputs = replace(slot_inputs(datetime(2026, 1, 11, 22, 10, tzinfo=LONDON), config),
                     pre_window_drain_kwh=1.0)
    plan = compute_plan(inputs, config, fixed_schedule(inputs, config))
    values = {p.at: p.soc_pct for p in plan.view.soc}
    assert values[inputs.geometry.window_start] == pytest.approx(max(
        config.min_soc,
        inputs.soc_now - plan.pre_window_drain_kwh / config.capacity_kwh * 100,
    ))
