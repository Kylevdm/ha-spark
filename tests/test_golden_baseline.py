"""Golden characterization tests for planner and backtest costs (P8.1, #35).

These pin today's exact plan choices and cost numbers under the fixed-window
two-rate tariff, ahead of the Phase 8 schedule re-costing (#36). If a later
change moves any of these numbers unintentionally, this file goes red.

The Phase 8 invariant: a `fixed`-provider schedule built from the same rates
and window must keep every assertion here passing unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.backtest import backtest_cost
from ha_spark.energy.models import (
    ConsumptionInterval,
    DispatchSlot,
    PlannerConfig,
    PlannerInputs,
)
from ha_spark.energy.planner import compute_plan

APPROX = 1e-6  # golden values recorded to 6 dp


def cfg(**kw: Any) -> PlannerConfig:
    base: dict[str, Any] = dict(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=0.85,
        window_start=time(23, 30),
        window_end=time(5, 30),
        rate_offpeak=0.069,
        rate_peak=0.30,
        buffer_pct=20.0,
        charge_efficiency=0.90,
    )
    base.update(kw)
    return PlannerConfig(**base)


HORIZON = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)

# 48 half-hour slots from the window start: lighter overnight, solar midday.
LOAD_SLOTS = tuple(0.4 if i < 12 else 0.5 for i in range(48))
SOLAR_SLOTS = tuple(1.0 if 20 <= i < 28 else 0.0 for i in range(48))


def test_golden_daily_zero_solar() -> None:
    """v1 daily balance, zero-solar day: full deficit, headroom-capped charge."""
    plan = compute_plan(
        PlannerInputs(soc_now=40, solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0),
        cfg(),
    )
    assert plan.model == "daily"
    assert plan.deficit_kwh == pytest.approx(24.0, abs=APPROX)
    assert plan.required_kwh == pytest.approx(13.44, abs=APPROX)
    assert plan.target_soc == pytest.approx(90.0, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(5.814, abs=APPROX)
    assert plan.planned_cost == pytest.approx(4.4396, abs=APPROX)
    assert plan.charge_intent.holds == ()
    assert plan.charge_intent.target_soc_pct == pytest.approx(plan.target_soc)


def test_golden_slots_no_dispatch() -> None:
    """v2 slot horizon, no-dispatch night: expensive-slot need drives sizing."""
    plan = compute_plan(
        PlannerInputs(
            soc_now=55,
            solar_tomorrow_kwh=8.0,
            predicted_home_load_kwh=sum(LOAD_SLOTS),
            load_slots=LOAD_SLOTS,
            solar_slots=SOLAR_SLOTS,
            horizon_start=HORIZON,
        ),
        cfg(),
    )
    assert plan.model == "slots"
    assert plan.deficit_kwh == pytest.approx(14.0, abs=APPROX)
    assert plan.expensive_load_kwh == pytest.approx(14.0, abs=APPROX)
    assert plan.required_kwh == pytest.approx(7.392, abs=APPROX)
    assert plan.target_soc == pytest.approx(82.5, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(4.5312, abs=APPROX)
    assert plan.planned_cost == pytest.approx(0.89792, abs=APPROX)
    assert plan.cheap_covered_kwh == pytest.approx(0.0, abs=APPROX)
    assert plan.charge_intent.holds == ()
    assert plan.dispatch_ev_kwh is None


def test_golden_slots_dispatch_overlap() -> None:
    """v2 slot horizon with an afternoon dispatch: overlap slots billed cheap."""
    dispatch = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 30, tzinfo=UTC),
        end=datetime(2026, 1, 16, 15, 0, tzinfo=UTC),
        charge_in_kwh=-5.2,
        source="octopus",
    )
    plan = compute_plan(
        PlannerInputs(
            soc_now=55,
            solar_tomorrow_kwh=8.0,
            predicted_home_load_kwh=sum(LOAD_SLOTS),
            load_slots=LOAD_SLOTS,
            solar_slots=SOLAR_SLOTS,
            horizon_start=HORIZON,
            dispatches=(dispatch,),
        ),
        cfg(),
    )
    assert plan.model == "slots"
    assert plan.deficit_kwh == pytest.approx(12.5, abs=APPROX)
    assert plan.required_kwh == pytest.approx(5.592, abs=APPROX)
    assert plan.target_soc == pytest.approx(75.803571, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(4.1847, abs=APPROX)
    assert plan.planned_cost == pytest.approx(0.86342, abs=APPROX)
    assert plan.cheap_covered_kwh == pytest.approx(1.5, abs=APPROX)
    assert plan.charge_intent.holds == ((dispatch.start, dispatch.end),)
    assert plan.dispatch_ev_kwh == pytest.approx(5.2, abs=APPROX)


def test_golden_daily_midnight_wrap_dispatch_classification() -> None:
    """v1 daily with the wrapping 23:30–05:30 window: a 02:00 dispatch counts as
    in-window (no hold, no cheap_covered), a 13:00 dispatch as daytime (hold)."""
    night = DispatchSlot(
        start=datetime(2026, 1, 16, 2, 0, tzinfo=UTC),
        end=datetime(2026, 1, 16, 4, 0, tzinfo=UTC),
        charge_in_kwh=-6.0,
    )
    day = DispatchSlot(
        start=datetime(2026, 1, 16, 13, 0, tzinfo=UTC),
        end=datetime(2026, 1, 16, 14, 30, tzinfo=UTC),
        charge_in_kwh=-2.4,
    )
    plan = compute_plan(
        PlannerInputs(
            soc_now=35,
            solar_tomorrow_kwh=6.0,
            predicted_home_load_kwh=22.0,
            dispatches=(night, day),
        ),
        cfg(),
    )
    assert plan.model == "daily"
    assert plan.cheap_covered_kwh == pytest.approx(1.375, abs=APPROX)
    assert plan.deficit_kwh == pytest.approx(15.525, abs=APPROX)
    assert plan.required_kwh == pytest.approx(14.598, abs=APPROX)
    assert plan.target_soc == pytest.approx(89.308036, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(3.481875, abs=APPROX)
    assert plan.planned_cost == pytest.approx(1.593555, abs=APPROX)
    assert plan.charge_intent.holds == ((day.start, day.end),)
    assert plan.dispatch_ev_kwh == pytest.approx(8.4, abs=APPROX)


def test_golden_fill_strategy_with_export_revenue() -> None:
    """Fill-to-cap on a sunny day with export: revenue offsets both projections
    identically (reporting honesty, no decision change)."""
    plan = compute_plan(
        PlannerInputs(soc_now=60, solar_tomorrow_kwh=30.0, predicted_home_load_kwh=12.0),
        cfg(rate_export=0.15, strategy="fill"),
    )
    assert plan.model == "daily"
    assert plan.deficit_kwh == pytest.approx(0.0, abs=APPROX)
    assert plan.required_kwh == pytest.approx(8.064, abs=APPROX)  # headroom to cap
    assert plan.target_soc == pytest.approx(90.0, abs=APPROX)
    assert plan.export_revenue == pytest.approx(2.025, abs=APPROX)
    assert plan.baseline_cost == pytest.approx(-2.025, abs=APPROX)
    assert plan.planned_cost == pytest.approx(-1.40676, abs=APPROX)


# The planner scenario matrix (dispatch overlap, solar) has no backtest
# analogue: backtest_cost is an actual-cost tally of stored grid import — it
# holds no dispatch or solar data (historic dispatches rate as peak, by
# documented design). The two-rate dimensions it does have are the rates and
# the (possibly midnight-wrapping) window, so both window shapes are pinned.


def _backtest_intervals(tz: ZoneInfo) -> list[ConsumptionInterval]:
    """96 half-hours (UTC store): 0.9 kWh in the 00:00–05:00 local hours, else 0.35."""
    start = datetime(2026, 1, 14, 22, 0, tzinfo=UTC)
    intervals = []
    for i in range(96):
        s = start + timedelta(minutes=30 * i)
        kwh = 0.9 if s.astimezone(tz).hour in (0, 1, 2, 3, 4) else 0.35
        intervals.append(ConsumptionInterval(start=s, end=s + timedelta(minutes=30), kwh=kwh))
    return intervals


def test_golden_backtest_two_rate_wrapping_window() -> None:
    """Backtest with the midnight-wrapping 23:30–05:30 window: pinned split."""
    tz = ZoneInfo("Europe/London")
    summary = backtest_cost(
        _backtest_intervals(tz),
        window_start=time(23, 30),
        window_end=time(5, 30),
        rate_offpeak=0.069,
        rate_peak=0.30,
        tz=tz,
    )
    assert summary is not None
    assert summary.days == 3
    assert summary.offpeak_kwh == pytest.approx(20.1, abs=APPROX)
    assert summary.peak_kwh == pytest.approx(24.5, abs=APPROX)
    assert summary.total_kwh == pytest.approx(44.6, abs=APPROX)
    assert summary.offpeak_cost == pytest.approx(20.1 * 0.069, abs=APPROX)
    assert summary.peak_cost == pytest.approx(24.5 * 0.30, abs=APPROX)
    assert summary.total_cost == pytest.approx(8.7369, abs=APPROX)


def test_golden_backtest_two_rate_non_wrapping_window() -> None:
    """Backtest with a non-wrapping 01:00–06:00 window: pinned split."""
    tz = ZoneInfo("Europe/London")
    summary = backtest_cost(
        _backtest_intervals(tz),
        window_start=time(1, 0),
        window_end=time(6, 0),
        rate_offpeak=0.069,
        rate_peak=0.30,
        tz=tz,
    )
    assert summary is not None
    assert summary.days == 3
    assert summary.offpeak_kwh == pytest.approx(16.5, abs=APPROX)
    assert summary.peak_kwh == pytest.approx(28.1, abs=APPROX)
    assert summary.total_cost == pytest.approx(9.5685, abs=APPROX)
