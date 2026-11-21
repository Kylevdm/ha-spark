"""Captured Free session evidence survives the public ledger round trip."""

from dataclasses import replace

import pytest
import respx

from ha_spark.energy.energy_ledger import EnergyLedger, capture_rates
from ha_spark.energy.models import FlexibilityEvent, FreeSession
from ha_spark.energy.tariff import apply_import_overlay
from tests.test_energy_ledger import CLOSE, HALF_HOUR, counter, settings_for
from tests.test_readiness_ledger import START, revision


@pytest.mark.parametrize(("kind", "winner", "expected"), [
    ("free_electricity", "hold", "hold"),
    ("happy_hour", "hold", "hold"),
    ("free_electricity", "axle", "axle_export"),
    ("happy_hour", "axle", "axle_export"),
    ("happy_hour", "negative", None),
    ("free_electricity", "negative", None),
])
@respx.mock
async def test_suppressed_session_evidence_is_recorded_for_hold_or_axle_only(
    tmp_path, kind, winner, expected,
):
    settings = settings_for(tmp_path)
    rev = revision()
    end = CLOSE + HALF_HOUR
    session = FreeSession(kind, "booking", START, end)
    inputs = replace(
        rev.run.inputs, free_sessions=(session,), happy_hour_remaining_kwh={"booking": 16},
        ev_hold_charging=winner == "hold",
        flexibility_event=FlexibilityEvent(START, CLOSE, "export", START, 1.5)
        if winner == "axle" else None,
    )
    schedule = rev.run.schedule
    if winner == "negative":
        schedule = replace(schedule, prices=(-0.2, *schedule.prices[1:]),
                           explicit_import_prices=(-0.2,))
    schedule = apply_import_overlay(schedule, inputs, rev.run.cfg)
    rev = replace(rev, run=replace(rev.run, inputs=inputs, schedule=schedule))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("12", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
    [row] = await ledger.slots(START, CLOSE)
    assert row.import_session == (kind if expected else None)
    assert row.session_suppressed_by == expected
    assert row.session_id == ("booking" if expected else None)
    assert (row.session_start, row.session_end) == ((START, end) if expected else (None, None))
    assert (row.import_rate, row.export_rate) == (-0.2 if winner == "negative" else 0.1, 0.05)


@pytest.mark.parametrize(("remaining", "expected"), [
    (2.0, 2.0), (17.0, 16.0), (None, None), (0.0, None), (-1.0, None),
    (float("nan"), None), (float("inf"), None), ("2", None), (True, None),
])
@respx.mock
async def test_no_purchase_room_captures_only_trusted_remaining_allowance(
    tmp_path, remaining, expected,
):
    settings = settings_for(tmp_path)
    rev = revision()
    session = FreeSession("happy_hour", "booking", START, CLOSE + HALF_HOUR)
    inputs = replace(
        rev.run.inputs, free_sessions=(session,), happy_hour_remaining_kwh={"booking": remaining},
        load_slots=(8,) * len(rev.run.schedule.prices),
    )
    schedule = apply_import_overlay(rev.run.schedule, inputs, rev.run.cfg)
    rev = replace(rev, run=replace(rev.run, inputs=inputs, schedule=schedule))
    ledger = EnergyLedger(settings.db_path)
    counter("sensor.grid_import", ("12", CLOSE))
    await ledger.record_boundary(settings, CLOSE, capture_rates(settings, CLOSE, rev))
    [row] = await ledger.slots(START, CLOSE)
    assert row.import_session == ("happy_hour" if expected else None)
    assert row.session_suppressed_by == ("no_purchase_room" if expected else None)
    assert row.session_remaining_kwh == expected
