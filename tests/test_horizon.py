"""Horizon geometry: slot 0 is the current half-hour (#395, first #394 ticket).

These pin the pure geometry's external behaviour: the spec's slot-count and
served-day tables, both DST transitions, a non-half-hour window boundary, a
24 h window, mid-slot slot-0 remaining time, and the legacy derivation that
keeps hand-built inputs on today's 48-slot layout.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.horizon import (
    SLOT,
    HorizonGeometry,
    compute_horizon_geometry,
    legacy_geometry,
)
from ha_spark.energy.models import PlannerConfig, PlannerInputs
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus

LONDON = ZoneInfo("Europe/London")

# 2026-01-11 is a Sunday, 2026-01-12 a Monday (GMT: no DST in January).
SUN = date(2026, 1, 11)
MON = date(2026, 1, 12)
TUE = date(2026, 1, 13)

W_2305 = (time(23, 30), time(5, 30))  # wraps midnight, 6 h
W_0030 = (time(0, 30), time(4, 30))  # no wrap, 4 h

APPROX = 1e-9


def at(day: date, h: int, m: int, tz: ZoneInfo = LONDON) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


# --- Spec tables: slot counts and served days ---------------------------------


@pytest.mark.parametrize(
    ("now", "n_slots", "served"),
    [
        (at(SUN, 23, 0), 49, MON),  # before the window: 24.5 h to the next window
        (at(SUN, 23, 30), 48, MON),  # exactly at the window start
        (at(MON, 1, 0), 45, MON),  # inside the running window
        (at(MON, 5, 0), 37, MON),  # last half-hour of the window
        (at(MON, 5, 30), 84, TUE),  # window closed: end moves to Tue 23:30
        (at(MON, 6, 0), 83, TUE),
    ],
)
def test_slot_counts_2330_window(now: datetime, n_slots: int, served: date) -> None:
    """Spec table for the 23:30-05:30 window: counts and served days."""
    geom = compute_horizon_geometry(now, *W_2305, LONDON)
    assert len(geom.slot_starts) == n_slots
    assert geom.served_day == served


@pytest.mark.parametrize(
    ("now", "served"),
    [
        (at(MON, 0, 0), MON),  # inside the running window
        (at(MON, 1, 0), MON),
        (at(MON, 4, 30), TUE),  # window closed: the next one serves Tuesday
    ],
)
def test_served_days_0030_window(now: datetime, served: date) -> None:
    """Spec served-day table for the non-wrapping 00:30-04:30 window."""
    geom = compute_horizon_geometry(now, *W_0030, LONDON)
    assert geom.served_day == served


def test_running_window_before_slot_0() -> None:
    """A replan inside the window carries the window's own start instant."""
    geom = compute_horizon_geometry(at(MON, 1, 0), *W_2305, LONDON)
    assert geom.horizon_start == at(MON, 1, 0)
    assert geom.window_start == at(SUN, 23, 30)
    assert geom.window_end == at(MON, 5, 30)
    # The horizon ends where the *following* window opens.
    assert geom.horizon_end == at(MON, 23, 30)
    assert len(geom.slot_starts) == 45
    # The window covers the 01:00-05:30 stretch: slots 0..8.
    assert geom.window_slot_range == (0, 9)
    assert geom.window_fracs[:9] == tuple(1.0 for _ in range(9))
    assert geom.window_fracs[9:] == tuple(0.0 for _ in range(36))


def test_window_after_the_running_one_closed() -> None:
    """Mon 05:30 (window just closed): horizon reaches to Tue 23:30."""
    geom = compute_horizon_geometry(at(MON, 5, 30), *W_2305, LONDON)
    assert geom.window_start == at(MON, 23, 30)
    assert geom.window_end == at(TUE, 5, 30)
    assert geom.horizon_end == at(TUE, 23, 30)
    assert len(geom.slot_starts) == 84
    assert geom.window_slot_range == (36, 48)  # Mon 23:30 through Tue 05:30
    assert sum(geom.window_fracs) == pytest.approx(12.0, abs=APPROX)  # 6 h


# --- Slot 0: the current half-hour ---------------------------------------------


def test_slot0_starts_at_the_current_half_hour() -> None:
    """A mid-slot replan: slot 0 is the running half-hour, prorated."""
    now = datetime(2026, 1, 12, 2, 17, tzinfo=LONDON)
    geom = compute_horizon_geometry(now, *W_2305, LONDON)
    assert geom.horizon_start == at(MON, 2, 0)
    assert geom.slot0_remaining_frac == pytest.approx(13.0 / 30.0, abs=APPROX)
    assert geom.slot_starts[0] == at(MON, 2, 0)
    # Slot starts are 30 *real* minutes apart, UTC arithmetic.
    assert geom.slot_starts[1] - geom.slot_starts[0] == SLOT
    assert geom.slot_starts[-1] < geom.horizon_end


def test_slot0_whole_at_a_boundary() -> None:
    """A replan exactly on the half-hour: slot 0 is whole."""
    geom = compute_horizon_geometry(at(SUN, 23, 0), *W_2305, LONDON)
    assert geom.slot0_remaining_frac == 1.0


# --- DST transitions -----------------------------------------------------------


def test_spring_forward_night() -> None:
    """Spring-forward (2026-03-29): the 24 h cycle is 23 real hours, 47 slots."""
    # Sat 2026-03-28 23:00 GMT; the window 23:30-05:30 spans the gap: 5 real h.
    now = datetime(2026, 3, 28, 23, 0, tzinfo=LONDON)
    geom = compute_horizon_geometry(now, *W_2305, LONDON)
    # 49 slots on an ordinary night, minus the skipped hour.
    assert len(geom.slot_starts) == 47
    # The window is real 5 h: Sat 23:30 GMT to Sun 05:30 BST.
    assert geom.window_start == datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    assert geom.window_end == datetime(2026, 3, 29, 4, 30, tzinfo=UTC)
    assert geom.window_slot_range == (1, 11)
    assert sum(geom.window_fracs) == pytest.approx(10.0, abs=APPROX)  # 5 h
    # The horizon ends at Sun 23:30 BST, 23.5 real hours away.
    assert geom.horizon_end == datetime(2026, 3, 29, 22, 30, tzinfo=UTC)
    assert geom.served_day == date(2026, 3, 29)


def test_fall_back_night() -> None:
    """Fall-back (2026-10-25): the 24 h cycle is 25 real hours, 51 slots."""
    # Sat 2026-10-24 23:00 BST; the window 23:30-05:30 spans the repeat: 7 real h.
    now = datetime(2026, 10, 24, 23, 0, tzinfo=LONDON)
    geom = compute_horizon_geometry(now, *W_2305, LONDON)
    # 49 slots on an ordinary night, plus the repeated hour.
    assert len(geom.slot_starts) == 51
    # The window is real 7 h: Sat 23:30 BST to Sun 05:30 GMT.
    assert geom.window_start == datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    assert geom.window_end == datetime(2026, 10, 25, 5, 30, tzinfo=UTC)
    assert geom.window_slot_range == (1, 15)
    assert sum(geom.window_fracs) == pytest.approx(14.0, abs=APPROX)  # 7 h
    assert geom.horizon_end == datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    assert geom.served_day == date(2026, 10, 25)


def test_nonexistent_boundary_resolves_forward() -> None:
    """A window start inside the spring-forward gap moves forward past it."""
    # Sun 2026-03-29 01:15 GMT wall time does not exist; 01:30 does not either.
    now = datetime(2026, 3, 29, 1, 15, tzinfo=UTC)  # local 02:15 BST
    geom = compute_horizon_geometry(now, time(1, 30), time(5, 30), LONDON)
    # fold=0: the nonexistent 01:30 resolves forward, to 02:30 BST.
    assert geom.window_start == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert geom.window_start.astimezone(LONDON).hour == 2
    # 3 real hours of window, 47 slots to the following Mon 01:30 BST.
    assert geom.window_slot_range == (1, 7)
    assert sum(geom.window_fracs) == pytest.approx(6.0, abs=APPROX)
    assert len(geom.slot_starts) == 47


def test_ambiguous_boundary_takes_first_occurrence() -> None:
    """A window start in the fall-back repeat takes its first occurrence."""
    # Sun 2026-10-25 01:30 happens twice: 01:30 BST then 01:30 GMT.
    now = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)  # local 01:15 BST (first pass)
    geom = compute_horizon_geometry(now, time(1, 30), time(5, 30), LONDON)
    assert geom.window_start == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert geom.window_start.astimezone(LONDON).fold == 0
    # 5 real hours of window (00:30-05:30 UTC), 51 slots to the following
    # Mon 01:30 GMT (the night gained an hour).
    assert geom.window_slot_range == (1, 11)
    assert sum(geom.window_fracs) == pytest.approx(10.0, abs=APPROX)
    assert len(geom.slot_starts) == 51


# --- Boundaries off the half-hour, and the 24 h window -------------------------


def test_non_half_hour_boundary_partial_coverage() -> None:
    """A 23:20-05:40 window: partial coverage in the first and last slots."""
    geom = compute_horizon_geometry(at(SUN, 23, 0), time(23, 20), time(5, 40), LONDON)
    assert geom.window_start == at(SUN, 23, 20)
    assert geom.window_end == at(MON, 5, 40)
    # Slot 0 overlaps the window by 10 of its 30 minutes.
    assert geom.window_fracs[0] == pytest.approx(1.0 / 3.0, abs=APPROX)
    # Slots 1..12 (23:30-05:30) are fully covered; slot 13 keeps 10 minutes.
    assert geom.window_fracs[1:13] == tuple(1.0 for _ in range(12))
    assert geom.window_fracs[13] == pytest.approx(1.0 / 3.0, abs=APPROX)
    assert geom.window_slot_range == (0, 14)
    # 6 h 20 m of window in 12 2/3 slots.
    assert sum(geom.window_fracs) == pytest.approx(38.0 / 3.0, abs=APPROX)
    # The horizon ends at Mon 23:20, 20 minutes inside the last (49th) slot.
    assert geom.horizon_end == at(MON, 23, 20)
    assert len(geom.slot_starts) == 49


def test_equal_start_and_end_is_a_24h_window() -> None:
    """A window whose start equals its end runs 24 h (window_hours' rule)."""
    w = (time(23, 30), time(23, 30))
    geom = compute_horizon_geometry(at(SUN, 23, 30), *w, LONDON)
    assert geom.window_start == at(SUN, 23, 30)
    assert geom.window_end == at(MON, 23, 30)
    assert geom.horizon_end == at(MON, 23, 30)  # the following window opens there
    assert len(geom.slot_starts) == 48
    assert geom.window_fracs == tuple(1.0 for _ in range(48))
    assert geom.window_slot_range == (0, 48)
    assert geom.served_day == MON


def test_24h_window_midday_replan() -> None:
    """Inside a 24 h window: the horizon ends where this window closes."""
    w = (time(23, 30), time(23, 30))
    geom = compute_horizon_geometry(at(SUN, 12, 0), *w, LONDON)
    assert geom.window_start == at(SUN - timedelta(days=1), 23, 30)  # Sat 23:30
    assert geom.window_end == at(SUN, 23, 30)
    assert geom.horizon_end == at(SUN, 23, 30)
    assert len(geom.slot_starts) == 23  # 11.5 h to the following window
    assert all(f == 1.0 for f in geom.window_fracs)
    assert geom.served_day == SUN


# --- Legacy derivation ---------------------------------------------------------


def _soc(value: float = 50.0) -> SocMeasurement:
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


def golden_cfg(**kw: Any) -> PlannerConfig:
    """The golden baseline's config (tests/test_golden_baseline.py `cfg`)."""
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


def test_legacy_geometry_is_todays_layout() -> None:
    """Hand-built horizon at a window start: today's exact 48-slot layout."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)  # the golden HORIZON
    geom = legacy_geometry(horizon, golden_cfg())
    assert geom.horizon_start == horizon
    assert len(geom.slot_starts) == 48
    assert geom.slot_starts == tuple(horizon + SLOT * i for i in range(48))
    # The window covers the first int(window_hours * 2) slots, whole.
    n_window = 12  # 6 h
    assert geom.window_fracs == tuple(1.0 if i < n_window else 0.0 for i in range(48))
    assert geom.window_slot_range == (0, n_window)
    assert geom.slot0_remaining_frac == 1.0
    # The window ends (and the served day turns) 6 real hours later; the
    # horizon ends where the following window opens, 48 slots on.
    assert geom.window_end == horizon + timedelta(hours=6)
    assert geom.horizon_end == horizon + SLOT * 48
    assert geom.served_day == date(2026, 1, 16)


def test_window_start_geometry_matches_the_legacy_layout() -> None:
    """A replan at the window start on an ordinary night is today's layout."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    live = compute_horizon_geometry(horizon, *W_2305, LONDON)
    assert live == legacy_geometry(horizon, golden_cfg())


def test_geometry_for_returns_the_supplied_geometry() -> None:
    """A supplied geometry is returned verbatim, not re-derived."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    geom = compute_horizon_geometry(horizon, *W_2305, LONDON)
    inputs = PlannerInputs(
        soc=_soc(), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=horizon, geometry=geom,
    )
    assert inputs.geometry_for(golden_cfg()) is geom


def test_geometry_for_derives_the_legacy_layout() -> None:
    """No geometry on hand-built inputs: the legacy one from the config."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    inputs = PlannerInputs(
        soc=_soc(), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
        load_slots=tuple(0.5 for _ in range(48)), horizon_start=horizon,
    )
    derived = inputs.geometry_for(golden_cfg())
    assert derived == legacy_geometry(horizon, golden_cfg())


def test_geometry_for_none_on_the_daily_path() -> None:
    """No horizon_start, no geometry (the v1 daily path): no geometry at all."""
    inputs = PlannerInputs(soc=_soc(), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=24.0)
    assert inputs.geometry_for(golden_cfg()) is None


def test_geometry_for_matches_short_slot_arrays() -> None:
    """An 8-slot fixture keeps its own length: today's indexing, 8 slots."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    inputs = PlannerInputs(
        soc=_soc(), solar_tomorrow_kwh=0.0, predicted_home_load_kwh=0.0,
        load_slots=(0.0,) * 8, horizon_start=horizon,
    )
    derived = inputs.geometry_for(golden_cfg())
    assert derived is not None
    assert len(derived.slot_starts) == 8
    # int(window_hours * 2) = 12 covers every slot the fixture has.
    assert derived.window_fracs == tuple(1.0 for _ in range(8))
    assert derived.window_slot_range == (0, 8)


def test_legacy_geometry_object_is_a_horizon_geometry() -> None:
    """The legacy derivation produces the same value type later tickets read."""
    horizon = datetime(2026, 1, 15, 23, 30, tzinfo=UTC)
    geom = legacy_geometry(horizon, golden_cfg())
    assert isinstance(geom, HorizonGeometry)
