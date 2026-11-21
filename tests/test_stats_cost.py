"""Cost, Coverage and Free sessions through the pure statistics seam."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.energy_ledger import ComponentFlow, LedgerSlot
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR
from ha_spark.energy.stats import compute_stats, cost_stats
from tests.test_readiness_ledger import START


def row(start=START, *, imported=3, exported=1, solar=2, house=6, **rates):
    components = {name: ComponentFlow(reason="not_configured") for name in COMPONENTS}
    for name, value in zip(COMPONENTS[:3], (imported, exported, solar), strict=True):
        if value is not None:
            components[name] = ComponentFlow(kwh=value, provenance="boundary")
    return LedgerSlot(
        slot_start=start, components=components, house_load_kwh=house, base_load_kwh=house,
        **({"import_rate": 0.25, "export_rate": 0.125} | rates),
    )


def happy_hour(start=START, **rates):
    return {
        "import_session": "happy_hour", "session_id": "booking", "session_start": start,
        "session_end": start + timedelta(hours=1),
    } | rates


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({}, (0.625, 1, 0.375)),
    ({"solar": 8}, (0.625, -0.25, -0.875)),
    ({"exported": None, "export_rate": 0}, (0.75, 1, 0.25)),
    ({"import_session": "free_electricity"}, (-0.125, 0, 0.125)),
    ({"import_session": "free_electricity", "session_suppressed_by": "hold"}, (-0.125, 0, 0.125)),
    ({"import_rate": -0.25}, (-0.875, -1, -0.125)),
])
def test_cost_formulas(kwargs, expected):
    result = cost_stats(compute_stats([row(**kwargs)], START, START + HALF_HOUR, UTC))
    [slot] = result.slots
    assert (slot.actual_cost, slot.no_battery_cost, slot.battery_savings) == expected
    assert slot.inputs.axle_earnings == 0
    assert (result.actual_cost, result.no_battery_cost, result.battery_savings) == expected
    assert result.cost_coverage == 1


@pytest.mark.parametrize(("rates", "component", "flow", "reason"), [
    ({"import_rate": None}, None, None, "rate_not_captured"),
    ({"export_rate": None}, None, None, "rate_not_captured"),
    ({"import_rate": None, "exclusions": ("provider_rate_unavailable",)}, None, None,
     "provider_rate_unavailable"),
    ({}, "grid_import", ComponentFlow(kwh=3, provenance="distributed"), "untrusted_grid_import"),
    ({}, "solar_generation", ComponentFlow(kwh=2, provenance="hourly_backfill"),
     "untrusted_solar_generation"),
    ({}, "grid_export", ComponentFlow(reason="counter_reset"), "untrusted_grid_export"),
    ({}, "battery_charge", ComponentFlow(kwh=3, provenance="distributed"),
     "untrusted_battery_charge"),
])
def test_cost_coverage_excludes_unpriced_energy(rates, component, flow, reason):
    second = row(START + HALF_HOUR, **rates)
    if component:
        second.components[component] = flow
    energy = compute_stats([row(), second], START, START + 2 * HALF_HOUR, UTC)
    result = cost_stats(energy)
    assert energy.energy_coverage == 1
    assert result.priced_slots == 1
    assert result.cost_coverage == 0.5
    assert result.slots[1].reason == reason
    assert result.slots[1].actual_cost is None
    assert (result.actual_cost, result.no_battery_cost, result.battery_savings) == (0.625, 1, 0.375)


def test_statistics_fill_prices_but_missing_house_load_does_not():
    first = row()
    first.components["grid_import"] = ComponentFlow(kwh=3, provenance="statistics_fill")
    second = row(START + HALF_HOUR, house=None)
    result = cost_stats(compute_stats([first, second], START, START + 2 * HALF_HOUR, UTC))
    assert result.slots[0].actual_cost == 0.625
    assert result.slots[1].reason == "missing_house_load"


@pytest.mark.parametrize("rate", [8e307, 1.6e308])
def test_non_finite_slot_or_total_is_unknown(rate):
    rows = [row(START + i * HALF_HOUR, imported=2, house=2, solar=0, import_rate=rate)
            for i in range(2)]
    rows.append(row(START + 2 * HALF_HOUR))  # Never report just the finite subset.
    result = cost_stats(compute_stats(rows, START, START + 3 * HALF_HOUR, UTC))
    assert result.cost_coverage == 1
    assert result.actual_cost is result.no_battery_cost is None
    if rate == 1.6e308:
        assert result.slots[0].actual_cost is result.battery_savings is None


def test_happy_hour_spends_separate_credits_in_slot_order_and_resets_each_hour():
    session = happy_hour(session_end=START + timedelta(hours=2))
    rows = [row(START + i * HALF_HOUR, imported=imp, house=load, solar=0, exported=0, **session)
            for i, (imp, load) in enumerate([(10, 12), (9, 10), (10, 12), (9, 10)])]
    result = cost_stats(compute_stats(reversed(rows), START, START + 4 * HALF_HOUR, UTC))
    assert [(s.inputs.free_import_kwh, s.inputs.no_battery_free_import_kwh)
            for s in result.slots] == [(10, 12), (6, 4), (10, 12), (6, 4)]
    assert [(s.actual_cost, s.no_battery_cost, s.battery_savings) for s in result.slots] == [
        (0, 0, 0), (0.75, 1.5, 0.75), (0, 0, 0), (0.75, 1.5, 0.75),
    ]


def test_happy_hour_counts_absolute_slots_when_the_household_clock_repeats():
    start = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    tz = ZoneInfo("Europe/London")
    rows = [row(start + i * HALF_HOUR, imported=10, house=10, solar=0, exported=0,
                **happy_hour(start)) for i in range(2)]
    result = cost_stats(compute_stats(rows, start, start + 2 * HALF_HOUR, tz))
    assert [slot.start.isoformat() for slot in result.slots] == [
        "2026-10-25T01:30:00+01:00", "2026-10-25T01:00:00+00:00",
    ]
    assert [slot.actual_cost for slot in result.slots] == [0, 1]


@pytest.mark.parametrize("earlier", ["out_of_span", "missing", "unpriced"])
def test_happy_hour_missing_earlier_coverage_fails_closed(earlier):
    midpoint = START + HALF_HOUR
    rows = [row(midpoint, **happy_hour())]
    if earlier == "unpriced":
        rows.append(row(import_rate=None, **happy_hour()))
    start = midpoint if earlier == "out_of_span" else START
    result = cost_stats(compute_stats(rows, start, midpoint + HALF_HOUR, UTC))
    assert result.slots[-1].reason == "happy_hour_earlier_slot_unpriced"
    assert result.cost_coverage == 0
    assert result.actual_cost is result.no_battery_cost is result.battery_savings is None


def test_no_purchase_room_caps_free_import_without_double_counting_earlier_use():
    rows = [
        row(imported=10, house=12, solar=0, exported=0, **happy_hour()),
        row(START + HALF_HOUR, imported=9, house=10, solar=0, exported=0,
            **happy_hour(session_suppressed_by="no_purchase_room", session_remaining_kwh=6)),
    ]
    result = cost_stats(compute_stats(rows, START, START + 2 * HALF_HOUR, UTC))
    assert result.actual_cost == 0.75
    assert result.no_battery_cost == 1.5


def test_no_purchase_room_credits_only_the_captured_remaining_allowance():
    first = row(imported=8, house=10, solar=0, exported=0,
                **happy_hour(session_suppressed_by="no_purchase_room", session_remaining_kwh=5))
    result = cost_stats(compute_stats([first], START, START + HALF_HOUR, UTC))
    assert (result.actual_cost, result.no_battery_cost, result.battery_savings) == (0.75, 1.25, 0.5)


def test_not_configured_counts_zero_only_when_absent():
    second = row(START + HALF_HOUR)
    second.components["solar_generation"] = ComponentFlow(
        provenance="hourly_backfill", reason="not_configured",
    )
    result = cost_stats(compute_stats([row(), second], START, START + 2 * HALF_HOUR, UTC))
    assert result.slots[1].reason == "untrusted_solar_generation"
    assert result.priced_slots == 1


def test_partial_session_slot_is_unpriced_with_its_exclusion():
    unpriced = row(import_rate=None, export_rate=None, exclusions=("partial_session_slot",))
    [slot] = cost_stats(compute_stats([unpriced], START, START + HALF_HOUR, UTC)).slots
    assert slot.reason == "partial_session_slot"


def test_no_purchase_room_without_a_captured_allowance_gets_no_credit():
    first = row(imported=8, house=10, solar=0, exported=0,
                **happy_hour(session_suppressed_by="no_purchase_room"))
    result = cost_stats(compute_stats([first], START, START + HALF_HOUR, UTC))
    assert result.slots[0].reason == "missing_happy_hour_allowance"
    assert result.priced_slots == 0
