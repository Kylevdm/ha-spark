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


@pytest.mark.parametrize("case", ["empty", "outside", "naive", "reversed", "happy_hour", "daily"])
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
    elif case == "happy_hour":
        session = replace(session, kind="happy_hour")
    inputs = slot_inputs(now, config, free_sessions=() if case == "empty" else (session,))
    if case == "daily":
        inputs = replace(inputs, load_slots=None)
    schedule = fixed_schedule(inputs, config)
    assert apply_import_overlay(schedule, inputs, config) is schedule
