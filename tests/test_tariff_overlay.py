"""Provider-independent battery import pricing, with base prices preserved."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ha_spark.config import Settings
from ha_spark.energy.models import FlexibilityEvent, FreeSession, PricePoint
from ha_spark.energy.sources import build_schedule
from ha_spark.energy.tariff import apply_import_overlay, fixed_schedule
from tests.test_planner import cfg
from tests.test_planner_slots_view import LONDON, slot_inputs


@pytest.mark.parametrize("provider", ["fixed", "dynamic", "octopus_intelligent"])
@pytest.mark.parametrize("axle", [False, True])
def test_overlay_runs_after_every_provider_and_preserves_export(provider, axle):
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, dynamic_prices=(
        PricePoint(now, now + timedelta(minutes=30), 0.25),
    ), flexibility_event=FlexibilityEvent(
        now + timedelta(hours=1), now + timedelta(hours=2), "export", now, 1.0,
    ))
    settings = Settings(tariff_provider=provider, export_event_provider="axle" if axle else "none")
    base = build_schedule(settings, inputs, config)
    inputs = replace(inputs, free_sessions=(
        FreeSession("free_electricity", "x", now, now + timedelta(hours=1)),
    ))
    result = build_schedule(settings, inputs, config)
    assert result.battery_import_prices[:2] == (0.0, 0.0)
    assert result.import_overlays[:3] == ("free_electricity", "free_electricity", None)
    assert result.import_allowances == (None,) * len(result.prices)
    assert replace(result, battery_import_prices=(), import_overlays=(), import_allowances=(),
                   import_sessions=()) == base


@pytest.mark.parametrize("case", ["empty", "outside", "naive", "reversed", "daily"])
def test_no_usable_session_returns_identical_schedule(case):
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    session = FreeSession("free_electricity", "x", now, now + timedelta(hours=1))
    if case == "outside":
        session = replace(session, start=now + timedelta(days=5), end=now + timedelta(days=6))
    elif case == "naive":
        session = replace(session, start=now.replace(tzinfo=None))
    elif case == "reversed":
        session = replace(session, end=now)
    inputs = slot_inputs(now, config, free_sessions=() if case == "empty" else (session,))
    if case == "daily":
        inputs = replace(inputs, load_slots=None)
    schedule = fixed_schedule(inputs, config)
    assert apply_import_overlay(schedule, inputs, config) is schedule


@pytest.mark.parametrize("remaining,expected", [(16, 12.4), (8, 4.4), (40, 12.4)])
def test_happy_hour_overlay_has_one_grid_allowance_after_gross_house_load(remaining, expected):
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    n = len(inputs.load_slots)
    inputs = replace(inputs, load_slots=(1.0,) * n, solar_slots=(1.0,) * n,
                     free_sessions=(FreeSession("happy_hour", "x", now,
                                                now + timedelta(hours=1)),),
                     happy_hour_remaining_kwh={"x": remaining})
    schedule = fixed_schedule(inputs, config)
    result = apply_import_overlay(schedule, inputs, config)
    assert result.battery_import_prices[:2] == (0.0, 0.0)
    assert result.import_overlays[:3] == ("happy_hour", "happy_hour", None)
    assert result.import_allowances[:2] == pytest.approx((expected, expected))
    assert result.prices == schedule.prices


def test_happy_hour_load_deduction_uses_remaining_real_overlap_even_in_claimed_slots():
    config = cfg()
    now = datetime(2026, 1, 12, 12, 15, tzinfo=LONDON)
    start = now.replace(minute=0)
    inputs = slot_inputs(now, config)
    n = len(inputs.load_slots)
    # Free Electricity claims the first slot, but its gross load still narrows
    # the booked hour's allowance. Only 15 minutes of that slot remain.
    inputs = replace(inputs, load_slots=(2.0,) * n, free_sessions=(
        FreeSession("free_electricity", "free", start, start + timedelta(minutes=30)),
        FreeSession("happy_hour", "happy", start, start + timedelta(hours=1)),),
        happy_hour_remaining_kwh={"happy": 16.0})
    result = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert result.import_overlays[:2] == ("free_electricity", "happy_hour")
    assert result.import_allowances[1] == pytest.approx(14.4 - 1 - 2)
    # Listing the Happy Hour first changes nothing: Free Electricity wins its slot.
    reordered = replace(inputs, free_sessions=inputs.free_sessions[::-1])
    assert apply_import_overlay(fixed_schedule(reordered, config), reordered, config) == result


def test_house_load_exhausting_happy_hour_allowance_claims_no_slot():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config)
    inputs = replace(inputs, load_slots=(8.0,) * len(inputs.load_slots), free_sessions=(
        FreeSession("happy_hour", "x", now, now + timedelta(hours=1)),),
        happy_hour_remaining_kwh={"x": 16.0})
    schedule = fixed_schedule(inputs, config)
    assert apply_import_overlay(schedule, inputs, config) is schedule


def test_free_electricity_survives_a_happy_hour_with_no_allowance():
    config = cfg()
    now = datetime(2026, 1, 12, 12, tzinfo=LONDON)
    inputs = slot_inputs(now, config, free_sessions=(
        FreeSession("free_electricity", "free", now, now + timedelta(hours=1)),
        FreeSession("happy_hour", "happy", now + timedelta(hours=1),
                    now + timedelta(hours=2)),
    ))
    result = apply_import_overlay(fixed_schedule(inputs, config), inputs, config)
    assert result.import_overlays[:3] == ("free_electricity", "free_electricity", None)
