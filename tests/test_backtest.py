"""Tests for the grid-import cost backtest."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.backtest import backtest_cost, format_backtest
from ha_spark.energy.horizon import compute_horizon_geometry
from ha_spark.energy.models import (
    ConsumptionInterval,
    DispatchSlot,
    PlannerConfig,
    PlannerInputs,
    PricePoint,
)
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.tariff import (
    DynamicTariffProvider,
    FixedTariffProvider,
    OctopusIntelligentProvider,
    TariffSchedule,
    fixed_schedule,
)

UTC_TZ = ZoneInfo("UTC")

# A bare fixed schedule: no per-slot prices, so backtest_cost falls back to the
# window derivation [window_start, window_start + window_hours) — the v1 path.
FIXED = {
    "schedule": TariffSchedule(
        cheap_rate=0.10,
        standard_rate=0.30,
        export_rate=0.0,
        window_hours=6.0,
        window_start=time(23, 30),
    )
}


def _iv(start: datetime, kwh: float) -> ConsumptionInterval:
    return ConsumptionInterval(start, start + timedelta(minutes=30), kwh)


def test_backtest_classifies_window_with_midnight_wrap() -> None:
    intervals = [
        _iv(datetime(2026, 6, 1, 23, 30, tzinfo=UTC), 1.0),  # in window (before midnight)
        _iv(datetime(2026, 6, 2, 3, 0, tzinfo=UTC), 2.0),  # in window (after midnight)
        _iv(datetime(2026, 6, 2, 12, 0, tzinfo=UTC), 4.0),  # peak
    ]
    s = backtest_cost(intervals, tz=UTC_TZ, **FIXED)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(3.0)
    assert s.peak_kwh == pytest.approx(4.0)
    assert s.total_kwh == pytest.approx(7.0)
    assert s.offpeak_cost == pytest.approx(0.30)
    assert s.peak_cost == pytest.approx(1.20)
    assert s.total_cost == pytest.approx(1.50)
    assert s.days == 2
    assert s.first == intervals[0].start
    assert s.last == intervals[-1].start


def test_backtest_uses_local_time_for_classification() -> None:
    # 22:45 UTC is 23:45 in London (BST) — inside the window locally.
    interval = _iv(datetime(2026, 6, 1, 22, 45, tzinfo=UTC), 1.0)
    s = backtest_cost([interval], tz=ZoneInfo("Europe/London"), **FIXED)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(1.0)
    assert s.peak_kwh == 0.0


def test_backtest_empty_returns_none() -> None:
    assert backtest_cost([], tz=UTC_TZ, **FIXED) is None


def test_backtest_dynamic_schedule_rates_by_cheap_fracs() -> None:
    """A dynamic schedule whose cheap slot lands mid-afternoon rates that slot
    off-peak — and an overnight-window slot that the schedule does *not* mark
    cheap now rates peak. Classification follows ``cheap_fracs``, not the clock
    window (which the old flat-rate backtest used)."""
    # Slot 0 of cheap_fracs is the 23:30 window start. 14:00 local is slot
    # (28 - 47) % 48 == 29; 02:00 local is slot (4 - 47) % 48 == 5.
    cheap_fracs = tuple(1.0 if i == 29 else 0.0 for i in range(48))
    schedule = TariffSchedule(
        cheap_rate=0.10,
        standard_rate=0.30,
        export_rate=0.0,
        window_hours=6.0,
        window_start=time(23, 30),
        prices=tuple(0.10 if i == 29 else 0.30 for i in range(48)),
        cheap_fracs=cheap_fracs,
    )
    intervals = [
        _iv(datetime(2026, 6, 2, 14, 0, tzinfo=UTC), 3.0),  # cheap mid-afternoon slot
        _iv(datetime(2026, 6, 2, 2, 0, tzinfo=UTC), 5.0),  # in the clock window, not cheap now
    ]
    s = backtest_cost(intervals, tz=UTC_TZ, schedule=schedule)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(3.0)
    assert s.peak_kwh == pytest.approx(5.0)


def test_backtest_dynamic_splits_fractional_slot() -> None:
    """A partially-cheap slot (supplier-controlled dispatch overlap) splits its
    energy proportionally between the off-peak and peak buckets."""
    cheap_fracs = tuple(0.25 if i == 29 else 0.0 for i in range(48))
    schedule = TariffSchedule(
        cheap_rate=0.10,
        standard_rate=0.30,
        export_rate=0.0,
        window_hours=6.0,
        window_start=time(23, 30),
        prices=tuple(0.30 for _ in range(48)),
        cheap_fracs=cheap_fracs,
    )
    s = backtest_cost(
        [_iv(datetime(2026, 6, 2, 14, 0, tzinfo=UTC), 4.0)], tz=UTC_TZ, schedule=schedule
    )
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(1.0)  # 0.25 * 4.0
    assert s.peak_kwh == pytest.approx(3.0)


def test_format_backtest_renders_totals() -> None:
    s = backtest_cost(
        [_iv(datetime(2026, 6, 2, 12, 0, tzinfo=UTC), 4.0)], tz=UTC_TZ, **FIXED
    )
    assert s is not None
    out = format_backtest(s)
    assert "1 days" in out
    assert "£   1.20" in out
    assert "(£1.20/day)" in out

# --- mid-day horizons (#399): classification stays anchored to the window -----

LONDON = ZoneInfo("Europe/London")
MON = date(2026, 1, 12)
TUE = date(2026, 1, 13)


def at(day: date, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LONDON)


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


def _cfg() -> PlannerConfig:
    return PlannerConfig(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=0.85,
        window_start=time(23, 30),
        window_end=time(5, 30),
    )


def _points(start: datetime, prices: list[float]) -> tuple[PricePoint, ...]:
    """One PricePoint per real half-hour from ``start`` (UTC), in order."""
    start = start.astimezone(UTC)
    return tuple(
        PricePoint(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            price=p,
        )
        for i, p in enumerate(prices)
    )


def _geom_inputs(
    now: datetime,
    points: tuple[PricePoint, ...] = (),
    dispatches: tuple[DispatchSlot, ...] = (),
) -> PlannerInputs:
    """Inputs on a real horizon geometry: slot 0 is the current half-hour."""
    geom = compute_horizon_geometry(now, time(23, 30), time(5, 30), LONDON)
    return PlannerInputs(
        soc=_soc(50), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0,
        now=now, horizon_start=geom.horizon_start, geometry=geom,
        load_slots=tuple(0.5 for _ in geom.slot_starts),
        dynamic_prices=points, dispatches=dispatches,
    )


def test_backtest_midday_dynamic_schedule_classifies_by_the_cycle() -> None:
    """A dynamic schedule built over a mid-day horizon (slot 0 = now, not the
    window start) classifies stored import exactly as the window-anchored
    schedule with the same cheap slots: the cycle view keeps the backtest on
    today's classification."""
    cycle_prices = [0.30] * 48
    for i in range(29, 41):  # Tue 14:00 through the afternoon: cheapest 12
        cycle_prices[i] = 0.05
    points = _points(at(MON, 23, 30), cycle_prices)
    sched = DynamicTariffProvider(FixedTariffProvider(0.10, 0.30, 0.0)).schedule(
        _geom_inputs(at(MON, 12, 0), points), _cfg()
    )
    legacy = TariffSchedule(
        cheap_rate=0.10,
        standard_rate=0.30,
        export_rate=0.0,
        window_hours=6.0,
        window_start=time(23, 30),
        prices=tuple(0.05 if 29 <= i < 41 else 0.30 for i in range(48)),
        cheap_fracs=tuple(1.0 if 29 <= i < 41 else 0.0 for i in range(48)),
    )
    intervals = [
        _iv(datetime(2026, 1, 13, 14, 0, tzinfo=UTC), 3.0),  # cheap afternoon slot
        _iv(datetime(2026, 1, 13, 2, 0, tzinfo=UTC), 5.0),  # in the clock window, unmarked
    ]
    midday = backtest_cost(intervals, tz=UTC_TZ, schedule=sched)
    anchored = backtest_cost(intervals, tz=UTC_TZ, schedule=legacy)
    assert midday is not None and anchored is not None
    assert midday.offpeak_kwh == pytest.approx(anchored.offpeak_kwh) == pytest.approx(3.0)
    assert midday.peak_kwh == pytest.approx(anchored.peak_kwh) == pytest.approx(5.0)


def test_backtest_fixed_midday_horizon_classifies_by_the_flat_window() -> None:
    """A fixed schedule over a mid-day horizon (no live prices) still
    classifies by the flat clock window — unchanged for a mid-day horizon."""
    sched = fixed_schedule(_geom_inputs(at(MON, 12, 0)), _cfg())
    intervals = [
        _iv(datetime(2026, 1, 12, 23, 30, tzinfo=UTC), 1.0),  # in window
        _iv(datetime(2026, 1, 13, 3, 0, tzinfo=UTC), 2.0),  # in window (after midnight)
        _iv(datetime(2026, 1, 13, 12, 0, tzinfo=UTC), 4.0),  # peak
    ]
    s = backtest_cost(intervals, tz=UTC_TZ, schedule=sched)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(3.0)
    assert s.peak_kwh == pytest.approx(4.0)


def test_backtest_intelligent_midday_horizon_classifies_by_the_cycle() -> None:
    """Intelligent (fixed folding + live prices) over a mid-day horizon: the
    daytime dispatch the cycle folds in rates off-peak, as it does today."""
    dispatch = DispatchSlot(start=at(TUE, 13, 0), end=at(TUE, 13, 30))
    points = _points(at(MON, 12, 0), [0.08] * 71)
    sched = OctopusIntelligentProvider(FixedTariffProvider(0.10, 0.30, 0.0)).schedule(
        _geom_inputs(at(MON, 12, 0), points, dispatches=(dispatch,)), _cfg()
    )
    intervals = [
        _iv(datetime(2026, 1, 13, 13, 0, tzinfo=UTC), 1.0),  # dispatch slot
        _iv(datetime(2026, 1, 13, 2, 0, tzinfo=UTC), 2.0),  # window slot
        _iv(datetime(2026, 1, 13, 12, 0, tzinfo=UTC), 4.0),  # peak
    ]
    s = backtest_cost(intervals, tz=UTC_TZ, schedule=sched)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(3.0)
    assert s.peak_kwh == pytest.approx(4.0)


def test_backtest_bins_dst_fallback_night_by_first_occurrence() -> None:
    """On the fall-back night the 25-hour cycle repeats the fold hour's local
    clocks; stored import bins by local clock time, first occurrence (decision
    C), so a mark on the repeat does not reach the first occurrence."""
    now = datetime(2026, 10, 24, 23, 30, tzinfo=LONDON)
    prices = [0.30] * 50
    prices[6] = 0.05  # cycle slot 6 = 01:30 GMT, the *second* 01:30 of the night
    for i in range(30, 41):
        prices[i] = 0.10  # eleven more cheap slots, far from the fold
    points = _points(now, prices)
    sched = DynamicTariffProvider(FixedTariffProvider(0.10, 0.30, 0.0)).schedule(
        _geom_inputs(now, points), _cfg()
    )
    intervals = [
        # 00:30 UTC = 01:30 BST: the first occurrence, unmarked -> peak.
        _iv(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), 1.0),
        # 01:30 UTC = 01:30 GMT: the marked repeat, but the bin takes the first
        # occurrence -> peak.
        _iv(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), 1.0),
        # 13:30 UTC: one of the 0.10-marked slots -> off-peak.
        _iv(datetime(2026, 10, 25, 13, 30, tzinfo=UTC), 2.0),
    ]
    s = backtest_cost(intervals, tz=LONDON, schedule=sched)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(2.0)
    assert s.peak_kwh == pytest.approx(2.0)


def test_backtest_bins_dst_springforward_night_by_real_slots() -> None:
    """On the spring-forward night the 23-hour cycle skips 01:30 local; a slot
    marked after the gap classifies the stored import that lands there."""
    now = datetime(2026, 3, 28, 23, 30, tzinfo=LONDON)
    prices = [0.30] * 46
    prices[4] = 0.05  # cycle slot 4 = 01:30 UTC = 02:30 BST (01:30 never happens)
    for i in range(30, 41):
        prices[i] = 0.10
    points = _points(now, prices)
    sched = DynamicTariffProvider(FixedTariffProvider(0.10, 0.30, 0.0)).schedule(
        _geom_inputs(now, points), _cfg()
    )
    intervals = [
        # 01:30 UTC = 02:30 BST: the marked post-gap slot -> off-peak.
        _iv(datetime(2026, 3, 29, 1, 30, tzinfo=UTC), 1.0),
        # 00:30 UTC = 00:30 GMT: unmarked -> peak.
        _iv(datetime(2026, 3, 29, 0, 30, tzinfo=UTC), 2.0),
    ]
    s = backtest_cost(intervals, tz=LONDON, schedule=sched)
    assert s is not None
    assert s.offpeak_kwh == pytest.approx(1.0)
    assert s.peak_kwh == pytest.approx(2.0)
