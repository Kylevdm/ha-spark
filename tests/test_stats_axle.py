"""Axle delivery and credits through the pure statistics seam."""

from datetime import UTC, timedelta

import pytest

from ha_spark.energy.axle_events import AxleEvent
from ha_spark.energy.energy_ledger import ComponentFlow
from ha_spark.energy.readiness_measurements import HALF_HOUR
from ha_spark.energy.stats import axle_outcomes, compute_stats, cost_stats
from tests.test_axle_events import ACCEPTED, END
from tests.test_stats_cost import START, row


def event(**changes):
    return AxleEvent.model_validate({
        "start": START, "end": END, "accepted_at": ACCEPTED,
        "committed_kwh": 3, "rate_gbp_kwh": 2,
    } | changes)


@pytest.mark.parametrize(("export", "expected", "delivery_rate"), [
    (2.7, "delivered", 1), (2.69, "partial", 0), (0.3, "partial", 0), (0.29, "missed", 0),
])
def test_delivery_thresholds_and_trip_reason(export, expected, delivery_rate):
    summary = axle_outcomes([event(stop_reason="tripped")], [row(exported=export),
                            row(START + HALF_HOUR, exported=0)])
    [outcome] = summary.events
    assert outcome.outcome == expected
    assert outcome.delivered_kwh == export
    assert outcome.earnings_gbp == 2 * export
    assert outcome.event.stop_reason == "tripped"
    assert summary.delivery_rate == delivery_rate


@pytest.mark.parametrize("gap", [
    "missing", "absent", "distributed", "hourly_backfill", "edge", "import", "unconfigured",
])
def test_incomplete_window_has_no_estimate_and_is_unpriced(gap):
    second = row(START + HALF_HOUR)
    if gap in {"absent", "distributed", "hourly_backfill"}:
        second.components["grid_export"] = ComponentFlow(
            kwh=None if gap == "absent" else 1, provenance=gap,
        )
    if gap == "import":  # A gap in any cost component voids the whole window.
        second.components["grid_import"] = ComponentFlow(kwh=1, provenance="distributed")
    if gap == "unconfigured":
        second.components["grid_export"] = ComponentFlow(
            kwh=None, provenance="absent", reason="not_configured",
        )
    rows = [row()] if gap == "missing" else [row(), second]
    events = [event(start=START + timedelta(minutes=5))] if gap == "edge" else [event()]
    costs = cost_stats(compute_stats(rows, START, END + HALF_HOUR, UTC), events)
    [outcome] = costs.axle.events
    assert outcome.outcome == "unknown"
    assert outcome.delivered_kwh is outcome.earnings_gbp is costs.axle.delivery_rate is None
    assert costs.axle_earnings_by_day == {START.date(): None}
    assert [s.reason for s in costs.slots] == [
        "axle_no_estimate", "axle_no_estimate", "missing_slot",
    ]


def test_cross_range_window_uses_full_measurements_but_credits_only_its_own_slots():
    first = row(exported=2)
    second = row(START + HALF_HOUR)
    second.components["grid_export"] = ComponentFlow(kwh=1, provenance="statistics_fill")
    stats = compute_stats([second, row(END)], START + HALF_HOUR, END + HALF_HOUR, UTC)
    costs = cost_stats(stats, [event()], event_rows=[first])
    priced, outside = costs.slots
    assert costs.axle.events[0].earnings_gbp == 6
    assert costs.axle.delivery_rate == 1
    assert costs.axle_earnings_by_day == {START.date(): 2}
    assert priced.inputs.axle_earnings == 2
    assert priced.actual_cost == -1.375
    assert priced.no_battery_cost == 1
    assert priced.battery_savings == 2.375
    assert outside.inputs.axle_earnings == 0


def test_cancelled_window_earns_no_credit_even_with_a_gap():
    stats = compute_stats([row()], START, END, UTC)
    costs = cost_stats(stats, [event(cancelled_at=START)])
    [outcome] = costs.axle.events
    assert outcome.outcome == "cancelled"
    assert outcome.earnings_gbp == 0
    assert costs.axle.delivery_rate is None
    assert costs.slots[0].actual_cost == 0.625
    assert costs.slots[1].reason == "missing_slot"


@pytest.mark.parametrize("committed", [None, 0])
def test_missing_commitment_excludes_event_from_delivery_rate_but_not_earnings(committed):
    summary = axle_outcomes([event(committed_kwh=committed)], [row(), row(START + HALF_HOUR)])
    assert summary.delivery_rate is None
    assert summary.events[0].outcome == "unknown"
    assert summary.events[0].earnings_gbp == 4


def test_unrepresentable_earnings_keep_the_measured_outcome():
    summary = axle_outcomes([event(rate_gbp_kwh=1e308)], [row(exported=2),
                            row(START + HALF_HOUR, exported=1)])
    [outcome] = summary.events
    assert (outcome.outcome, outcome.delivered_kwh, outcome.earnings_gbp) == ("delivered", 3, None)
    assert summary.delivery_rate == 1


@pytest.mark.parametrize("resolution", ["slot", "day"])
def test_ranges_carry_axle_credit_daily_earnings_and_delivery_rate(resolution):
    from ha_spark.energy.stats import summarise

    rows = [row(exported=2), row(START + HALF_HOUR, exported=1)]
    stats = summarise(rows, START, START + timedelta(days=1), UTC, resolution, [event()])
    assert stats.axle_earnings_by_day == ((START.date(), 6),)
    assert stats.axle.delivery_rate == 1
    plain = summarise(rows, START, START + timedelta(days=1), UTC, resolution)
    assert stats.totals.actual_cost == plain.totals.actual_cost - 6
    assert stats.totals.no_battery_cost == plain.totals.no_battery_cost
