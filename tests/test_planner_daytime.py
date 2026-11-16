"""Free Electricity purchases through the pure planner and its owner-facing view."""
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from ha_spark.energy.models import FreeSession, PricePoint
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import (
    DynamicTariffProvider,
    FixedTariffProvider,
    apply_import_overlay,
    fixed_schedule,
)
from tests.test_planner import _soc, _soc_unavailable, cfg
from tests.test_planner_limits import config as limits_config
from tests.test_planner_limits import native
from tests.test_planner_reserve import site
from tests.test_planner_slots_view import LONDON, slot_inputs


def planned(inputs, config, **kwargs):
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    return compute_plan(inputs, config, schedule, **kwargs)


def test_free_session_fills_cap_and_shrinks_next_overnight():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    inputs = replace(inputs, load_slots=(0.0,) * len(inputs.load_slots),
                     solar_slots=(0.0,) * len(inputs.load_slots))
    ordinary = planned(inputs, config)
    session = FreeSession("free_electricity", "private-id", now, now + timedelta(hours=6))
    plan = planned(replace(inputs, free_sessions=(session,)), config)
    purchase, = plan.daytime_purchases
    assert purchase.energy_kwh == pytest.approx(ordinary.required_kwh)
    assert plan.required_kwh == pytest.approx(0)
    assert plan.target_soc == config.target_cap
    assert plan.charge_intent.daytime_windows[0].target_soc_pct == config.target_cap
    assert "Free Electricity" in purchase.reason
    assert "listed session" in purchase.reason
    assert "£0" in purchase.reason
    assert "private-id" not in purchase.reason
    assert any(line.text == purchase.reason for line in plan.view.lines)
    assert "charge" in plan.view.slots[0].actions
    assert max(point.soc_pct for point in plan.view.soc) == pytest.approx(config.target_cap)


def test_post_window_session_does_not_reduce_prior_purchase():
    config = cfg()
    now = datetime(2026, 1, 12, 0, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    topology = native(config)
    ordinary = planned(inputs, config, topology=topology)
    assert ordinary.inverter_allocations
    start = now.replace(hour=12)
    plan = planned(replace(inputs, free_sessions=(
        FreeSession("free_electricity", "x", start, start + timedelta(hours=1)),
    )), config, topology=topology)
    assert plan.daytime_purchases
    for name in ("required_kwh", "target_soc", "inverter_allocations"):
        assert getattr(plan, name) == getattr(ordinary, name)
    assert plan.charge_intent.target_soc_pct == ordinary.charge_intent.target_soc_pct
    assert (plan.charge_intent.expected_window_start_soc
            == ordinary.charge_intent.expected_window_start_soc)


@pytest.mark.parametrize("now,start,end,hours", [
    (datetime(2026, 1, 12, 12, 17, tzinfo=LONDON),
     datetime(2026, 1, 12, 12, tzinfo=LONDON),
     datetime(2026, 1, 12, 13, tzinfo=LONDON), 43 / 60),
    (datetime(2026, 1, 12, 22, tzinfo=LONDON),
     datetime(2026, 1, 12, 23, 30, tzinfo=LONDON),
     datetime(2026, 1, 13, 0, 30, tzinfo=LONDON), 1),
    (datetime(2026, 3, 29, 0, tzinfo=LONDON),
     datetime(2026, 3, 29, 0, 30, tzinfo=LONDON),
     datetime(2026, 3, 29, 2, 30, tzinfo=LONDON), 1),
    (datetime(2026, 10, 25, 0, tzinfo=LONDON),
     datetime(2026, 10, 25, 0, 30, tzinfo=LONDON),
     datetime(2026, 10, 25, 2, 30, tzinfo=LONDON), 3),
])
def test_remaining_real_time_across_midnight_and_clock_changes(now, start, end, hours):
    # Place the overnight window after all these sessions.
    config = cfg(window_start=time(4), window_end=time(5), capacity_kwh=100)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", start, end),
    ))
    inputs = replace(inputs, load_slots=(0.0,) * len(inputs.load_slots),
                     solar_slots=(0.0,) * len(inputs.load_slots))
    plan = planned(inputs, config)
    purchase, = plan.daytime_purchases
    assert purchase.start == max(now.astimezone(UTC), start.astimezone(UTC))
    assert purchase.end == end.astimezone(UTC)
    assert purchase.energy_kwh == pytest.approx(config.voltage_v * config.max_current_a / 1000
                                               * hours)
    window, = plan.charge_intent.daytime_windows
    assert (window.start, window.end) == (purchase.start, purchase.end)


@pytest.mark.parametrize("kind,expected", [("device", 3.25), ("bank", 0.52),
                                           ("connection", 1.12), ("reserve", 0.96)])
def test_daytime_shares_binding_overnight_limits(kind, expected):
    config = limits_config()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    inputs = replace(inputs, load_slots=(0.5,) * len(inputs.load_slots),
                     solar_slots=(0.0,) * len(inputs.load_slots))
    topology = (site() if kind == "reserve" else native(
        config, bank_a=10 if kind == "bank" else 100,
        import_a=10 if kind == "connection" else 75,
    ))
    plan = planned(inputs, config, topology=topology)
    assert plan.daytime_purchases[0].energy_kwh == pytest.approx(expected)


def test_house_costs_base_price_while_battery_import_is_free():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    n = len(inputs.load_slots)
    inputs = replace(inputs, load_slots=(0.5, 0.5) + (0.0,) * (n - 2),
                     solar_slots=(0.0,) * n)
    plan = planned(inputs, config)
    assert plan.baseline_cost == pytest.approx(config.rate_peak)
    assert plan.planned_cost == pytest.approx(
        config.rate_peak + plan.required_kwh / config.charge_efficiency * config.rate_offpeak)
    # House load did not drain the battery during the session.
    at_end = next(p for p in plan.view.soc if p.at == now + timedelta(hours=1))
    assert at_end.soc_pct == pytest.approx(inputs.soc_now +
                                         plan.daytime_purchases[0].energy_kwh
                                         / config.capacity_kwh * 100)


def test_daytime_energy_is_counted_once_at_the_next_window():
    config = cfg(buffer_pct=0)
    now = datetime(2026, 1, 12, 20, tzinfo=LONDON)
    inputs = replace(slot_inputs(now, config), soc=_soc(50))
    ordinary = planned(inputs, config)
    session = FreeSession("free_electricity", "x", now, now + timedelta(hours=1))
    plan = planned(replace(inputs, free_sessions=(session,)), config)
    bought = plan.daytime_purchases[0].energy_kwh
    # Besides the stored purchase, the battery kept the session's house load.
    kept = 2 * (0.6 - 0.2 * config.solar_haircut_k)
    assert ordinary.required_kwh - plan.required_kwh == pytest.approx(bought + kept)
    assert (plan.charge_intent.expected_window_start_soc
            - ordinary.charge_intent.expected_window_start_soc) == pytest.approx(
                (bought + kept) / config.capacity_kwh * 100)


def test_adjacent_sessions_have_separate_purchases_and_share_headroom():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "first", now, now + timedelta(hours=1)),
        FreeSession("free_electricity", "second", now + timedelta(hours=1),
                    now + timedelta(hours=2)),
    ))
    inputs = replace(inputs, soc=_soc(75), load_slots=(0.0,) * len(inputs.load_slots),
                     solar_slots=(0.0,) * len(inputs.load_slots))
    plan = planned(inputs, config)
    assert [p.session_id for p in plan.daytime_purchases] == ["first", "second"]
    assert sum(p.energy_kwh for p in plan.daytime_purchases) == pytest.approx(
        config.capacity_kwh * (config.target_cap - inputs.soc_now) / 100)
    assert plan.charge_intent.daytime_windows[-1].target_soc_pct == config.target_cap


def test_legacy_horizon_supports_only_post_window_sessions_and_daily_has_none():
    config = cfg()
    now = datetime(2026, 1, 12, 23, 30, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now + timedelta(hours=12),
                    now + timedelta(hours=13)),
    ))
    inputs = replace(inputs, geometry=None)
    assert planned(inputs, config).daytime_purchases
    inputs = replace(inputs, load_slots=None, solar_slots=None)
    assert planned(inputs, config).daytime_purchases == ()


def test_charge_window_slots_are_not_daytime_purchases():
    config = cfg()
    now = datetime(2026, 1, 12, 0, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    assert planned(inputs, config).daytime_purchases == ()


def test_session_sized_from_schedule_not_raw_eligibility():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    assert compute_plan(inputs, config, fixed_schedule(inputs, config)).daytime_purchases == ()


def test_partial_session_soc_rises_at_its_actual_end():
    config = cfg(strategy="fill")
    now = datetime(2026, 1, 12, 0, tzinfo=LONDON)
    start, end = now + timedelta(hours=12, minutes=10), now + timedelta(hours=12, minutes=20)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", start, end),
    ))
    plan = planned(inputs, config)
    points = {p.at: p.soc_pct for p in plan.view.soc}
    assert points[end] > points[start]
    assert max(points.values()) <= config.target_cap


def test_outside_horizon_is_ignored_by_plan():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    ordinary = planned(inputs, config)
    assert planned(replace(inputs, free_sessions=(FreeSession(
        "free_electricity", "x", now + timedelta(days=4), now + timedelta(days=5),
    ),)), config) == ordinary


@pytest.mark.parametrize("failed_soc", [False, True])
def test_untrusted_inputs_never_purchase(failed_soc):
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    if failed_soc:
        inputs = replace(inputs, soc=_soc_unavailable())
    plan = planned(inputs, config, **({} if failed_soc else {"limits_unavailable": "unavailable"}))
    assert plan.daytime_purchases == ()
    assert plan.charge_intent.daytime_windows == ()


def test_pre_window_export_is_spent_once_beside_a_session():
    from tests.test_planner import _current_event_inputs, _same_day_cfg, _same_day_inputs

    config = replace(_same_day_cfg(), buffer_pct=0)
    now = datetime(2026, 6, 8, 16, 0, tzinfo=UTC)
    inputs = _current_event_inputs(_same_day_inputs(soc_now=60.0, now=now), config)
    ordinary = planned(inputs, config)
    assert ordinary.charge_intent.export is not None
    for start in (now, now + timedelta(hours=5)):  # before and after the export event
        plan = planned(replace(inputs, free_sessions=(
            FreeSession("free_electricity", "x", start, start + timedelta(minutes=30)),
        )), config)
        assert plan.charge_intent.export == ordinary.charge_intent.export
        bought = plan.daytime_purchases[0].energy_kwh
        # The session adds what it bought and keeps its own half-hour of house load.
        assert ordinary.required_kwh - plan.required_kwh == pytest.approx(
            bought + 0.25)


def test_post_window_session_leaves_reservations_and_drain_alone():
    config = cfg()
    now = datetime(2026, 1, 12, 20, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    ordinary = planned(inputs, config)
    start = datetime(2026, 1, 13, 12, tzinfo=LONDON)
    plan = planned(replace(inputs, free_sessions=(
        FreeSession("free_electricity", "x", start, start + timedelta(hours=1)),
    )), config)
    assert plan.daytime_purchases
    for name in ("required_kwh", "target_soc", "reservations", "pre_window_drain_kwh",
                 "charge_funded_reservations"):
        assert getattr(plan, name) == getattr(ordinary, name)
    assert replace(plan.charge_intent, daytime_windows=()) == ordinary.charge_intent


def test_short_overlay_arrays_and_unpriced_schedules_never_purchase():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    schedule = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    short = replace(schedule, import_sessions=schedule.import_sessions[:1])
    assert compute_plan(inputs, config, short).daytime_purchases == ()
    unpriced = replace(fixed_schedule(inputs, config), prices=())
    assert apply_import_overlay(unpriced, inputs, config) is unpriced


# --- dynamic live prices as purchase evidence (#696) ---


def _price_points(start: datetime, prices: list[float]) -> tuple[PricePoint, ...]:
    """One half-hourly PricePoint per slot from ``start``, in order."""
    start = start.astimezone(UTC)
    return tuple(
        PricePoint(start + timedelta(minutes=30 * i),
                   start + timedelta(minutes=30 * (i + 1)), price)
        for i, price in enumerate(prices)
    )


def _dynamic_schedule(inputs, config):
    return DynamicTariffProvider(FixedTariffProvider(
        config.rate_offpeak, config.rate_peak, config.rate_export,
    )).schedule(inputs, config)


def _dynamic_inputs(now, config, prices: list[float]):
    """Zero-load inputs whose horizon carries one explicit live price per slot."""
    inputs = slot_inputs(now, config)
    n = len(inputs.geometry.slot_starts)
    return replace(
        inputs,
        load_slots=(0.0,) * n,
        solar_slots=(0.0,) * n,
        dynamic_prices=_price_points(inputs.geometry.horizon_start, prices),
    )


def test_dynamic_buy_below_overnight_shrinks_the_next_purchase():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = _dynamic_inputs(now, config, [0.05] + [0.30] * 70)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.30)
    ordinary = compute_plan(
        inputs, config, replace(schedule, explicit_import_prices=(None,) * len(schedule.prices)))
    plan = compute_plan(inputs, config, schedule)
    purchase, = plan.daytime_purchases
    assert purchase.effective_price == pytest.approx(0.05)
    assert purchase.overlay == "dynamic_price"
    assert purchase.start == now.astimezone(UTC)
    assert purchase.end == now.astimezone(UTC) + timedelta(minutes=30)
    assert "£0.05/kWh" in purchase.reason
    assert "£0.3/kWh average overnight price" in purchase.reason
    assert ordinary.required_kwh - plan.required_kwh == pytest.approx(purchase.energy_kwh)


@pytest.mark.parametrize("price", [0.30, 0.31])
def test_dynamic_slot_at_or_above_the_overnight_price_is_not_bought(price):
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = _dynamic_inputs(now, config, [price] + [0.30] * 70)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.30)
    assert compute_plan(inputs, config, schedule).daytime_purchases == ()


def test_dynamic_comparison_uses_the_selected_overnight_average():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    # Six selected overnight slots at £0.05 and six at £0.25 average £0.15,
    # well above the £0.069 fixed fallback. A £0.10 daytime slot is above the
    # fallback but below that selected average, so it is still bought.
    prices = [0.10] + [0.20] * 22 + [0.05] * 6 + [0.25] * 42
    inputs = _dynamic_inputs(now, config, prices)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.15)
    purchase, = compute_plan(inputs, config, schedule).daytime_purchases
    assert purchase.effective_price == pytest.approx(0.10)
    assert "£0.15/kWh average overnight price" in purchase.reason
    assert "£0.069/kWh" not in purchase.reason


def test_dynamic_slot_at_the_selected_average_is_not_bought():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    prices = [0.15] + [0.20] * 22 + [0.05] * 6 + [0.25] * 42
    inputs = _dynamic_inputs(now, config, prices)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.15)
    assert compute_plan(inputs, config, schedule).daytime_purchases == ()


def test_dynamic_average_below_fallback_blocks_a_slot_above_it():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    # Six selected overnight slots at £0.03 and six at £0.05 average £0.04,
    # below the £0.069 fixed fallback. A £0.05 daytime slot beats the fallback
    # but not the selected average, so it must not be bought.
    prices = [0.05] * 23 + [0.03] * 6 + [0.05] * 6 + [0.20] * 36
    inputs = _dynamic_inputs(now, config, prices)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.04)
    assert compute_plan(inputs, config, schedule).daytime_purchases == ()


def test_fallback_filled_price_is_never_purchase_evidence():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    # Only slot 0 has a live read; every other slot's cost is the fallback
    # standard rate, which must never count as an explicit price.
    inputs = slot_inputs(now, config, dynamic_prices=(
        PricePoint(now, now + timedelta(minutes=30), 0.05),
    ))
    n = len(inputs.geometry.slot_starts)
    inputs = replace(inputs, load_slots=(0.0,) * n, solar_slots=(0.0,) * n)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.prices[1] == pytest.approx(config.rate_peak)
    assert schedule.explicit_import_prices[1] is None
    assert compute_plan(inputs, config, schedule).daytime_purchases
    no_evidence = replace(schedule, explicit_import_prices=(None,) * len(schedule.prices))
    assert compute_plan(inputs, config, no_evidence).daytime_purchases == ()


def test_negative_dynamic_price_is_bought_and_costed_negative():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = _dynamic_inputs(now, config, [-0.05] + [0.30] * 70)
    schedule = _dynamic_schedule(inputs, config)
    ordinary = compute_plan(
        inputs, config, replace(schedule, explicit_import_prices=(None,) * len(schedule.prices)))
    plan = compute_plan(inputs, config, schedule)
    purchase, = plan.daytime_purchases
    assert purchase.effective_price == pytest.approx(-0.05)
    assert purchase.energy_kwh > 0
    assert "£0.3/kWh average overnight price" in purchase.reason
    # The purchase displaces the overnight buy at ``cheap_rate`` and is itself
    # costed at its negative price.
    assert ordinary.planned_cost - plan.planned_cost == pytest.approx(
        purchase.energy_kwh / config.charge_efficiency
        * (schedule.cheap_rate - purchase.effective_price))


def test_fixed_schedule_carries_no_explicit_dynamic_prices():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    assert fixed_schedule(slot_inputs(now, config), config).explicit_import_prices == ()


def test_expired_price_interval_is_not_purchase_evidence():
    """An expired read overlapping slot 0 must not price the rest of the slot."""
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, 17, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    n = len(inputs.geometry.slot_starts)
    next_slot = now.replace(minute=30, second=0, microsecond=0)
    inputs = replace(
        inputs,
        load_slots=(0.0,) * n,
        solar_slots=(0.0,) * n,
        dynamic_prices=(
            PricePoint(now.replace(minute=0), now.replace(minute=10), 0.05),
            PricePoint(next_slot, next_slot + timedelta(minutes=30), 0.05),
        ),
    )
    schedule = _dynamic_schedule(inputs, config)
    # Costing still projects the overlapping read onto slot 0; evidence does not.
    assert schedule.prices[0] == pytest.approx(0.05)
    assert schedule.explicit_import_prices[0] is None
    purchase, = compute_plan(inputs, config, schedule).daytime_purchases
    assert purchase.start == next_slot.astimezone(UTC)
    assert purchase.effective_price == pytest.approx(0.05)


def test_partially_covered_future_slot_is_not_purchase_evidence():
    """A read covering only part of a future slot must not price the whole slot."""
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    n = len(inputs.geometry.slot_starts)
    partial = now.replace(minute=30, second=0, microsecond=0)
    inputs = replace(
        inputs,
        load_slots=(0.0,) * n,
        solar_slots=(0.0,) * n,
        dynamic_prices=(
            PricePoint(now, now + timedelta(minutes=30), 0.30),
            PricePoint(partial, partial + timedelta(minutes=10), 0.05),
        ),
    )
    schedule = _dynamic_schedule(inputs, config)
    # Costing keeps its overlap projection, but a partial read is not evidence.
    assert schedule.prices[1] == pytest.approx(0.05)
    assert schedule.explicit_import_prices[1] is None
    assert compute_plan(inputs, config, schedule).daytime_purchases == ()


def test_partial_current_slot_buys_remaining_time_and_projects_soc():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, 17, tzinfo=LONDON)
    inputs = _dynamic_inputs(now, config, [0.05] + [0.30] * 70)
    schedule = _dynamic_schedule(inputs, config)
    ordinary = compute_plan(
        inputs, config, replace(schedule, explicit_import_prices=(None,) * len(schedule.prices)))
    plan = compute_plan(inputs, config, schedule)
    purchase, = plan.daytime_purchases
    slot_end = now.replace(minute=30, second=0, microsecond=0)
    hours = (slot_end - now).total_seconds() / 3600
    expected = config.voltage_v * config.max_current_a / 1000 * hours
    assert purchase.start == now.astimezone(UTC)
    assert purchase.end == slot_end.astimezone(UTC)
    assert purchase.energy_kwh == pytest.approx(expected)
    assert ordinary.required_kwh - plan.required_kwh == pytest.approx(expected)
    at_end = next(p for p in plan.view.soc if p.at == slot_end.astimezone(UTC))
    assert at_end.soc_pct == pytest.approx(
        inputs.soc_now + expected / config.capacity_kwh * 100)
    assert ordinary.planned_cost - plan.planned_cost == pytest.approx(
        expected / config.charge_efficiency * (schedule.cheap_rate - purchase.effective_price))


def test_repeated_cheap_slots_never_buy_beyond_the_target_cap():
    config = cfg(strategy="fill", buffer_pct=0)
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    # Every pre-window slot is £0.05; all selected overnight slots are £0.10.
    inputs = _dynamic_inputs(now, config, [0.05] * 23 + [0.10] * 48)
    schedule = _dynamic_schedule(inputs, config)
    assert schedule.overnight_average_price == pytest.approx(0.10)
    plan = compute_plan(inputs, config, schedule)
    usable_cap = config.capacity_kwh * (config.target_cap - config.min_soc) / 100
    assert len(plan.daytime_purchases) > 1
    assert sum(p.energy_kwh for p in plan.daytime_purchases) == pytest.approx(
        usable_cap - (inputs.soc_now - config.min_soc) / 100 * config.capacity_kwh)
    assert max(point.soc_pct for point in plan.view.soc) <= config.target_cap + 1e-9
    assert plan.required_kwh == pytest.approx(0)
