"""Tests for the slot-of-day load profile."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval
from ha_spark.energy.profile import build_slot_profile, predict_day_slots

LONDON = ZoneInfo("Europe/London")
UTC_TZ = ZoneInfo("UTC")


def _day(d: date, kwh: float, tz: ZoneInfo = UTC_TZ) -> list[ConsumptionInterval]:
    """A full day of half-hour readings, each ``kwh``."""
    start = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC)
    return [
        ConsumptionInterval(
            start=start + timedelta(minutes=30 * i),
            end=start + timedelta(minutes=30 * (i + 1)),
            kwh=kwh,
        )
        for i in range(SLOTS_PER_DAY)
    ]


def _history(start: date, days: int, weekday_kwh: float, weekend_kwh: float) -> list[
    ConsumptionInterval
]:
    rows: list[ConsumptionInterval] = []
    for i in range(days):
        d = start + timedelta(days=i)
        rows.extend(_day(d, weekend_kwh if d.weekday() >= 5 else weekday_kwh))
    return rows


def test_medians_split_by_day_type() -> None:
    # 2026-06-01 is a Monday; 14 days covers 4 weekend dates.
    rows = _history(date(2026, 6, 1), 14, weekday_kwh=0.4, weekend_kwh=0.8)
    profile = build_slot_profile(rows, UTC_TZ, min_days=7)
    assert profile is not None
    assert profile.days_used == 14
    assert all(v == 0.4 for v in profile.weekday)
    assert all(v == 0.8 for v in profile.weekend)
    assert predict_day_slots(profile, date(2026, 6, 15)) == profile.weekday  # Monday
    assert predict_day_slots(profile, date(2026, 6, 20)) == profile.weekend  # Saturday


def test_insufficient_history_returns_none() -> None:
    rows = _history(date(2026, 6, 1), 5, 0.4, 0.8)  # Mon-Fri: no weekend days at all
    assert build_slot_profile(rows, UTC_TZ, min_days=7) is None
    rows = _history(date(2026, 6, 1), 6, 0.4, 0.8)  # 6 days, 1 weekend date
    assert build_slot_profile(rows, UTC_TZ, min_days=3) is None


def test_holes_filled_from_other_day_type() -> None:
    # Weekend days only have readings for slot 0; other weekend slots must
    # fall back to the weekday median, not be zero/missing.
    rows = _history(date(2026, 6, 1), 5, weekday_kwh=0.4, weekend_kwh=0.0)
    sat = date(2026, 6, 6)
    sun = date(2026, 6, 7)
    for d in (sat, sun):
        start = datetime(d.year, d.month, d.day, tzinfo=UTC)
        rows.append(
            ConsumptionInterval(start=start, end=start + timedelta(minutes=30), kwh=1.0)
        )
    profile = build_slot_profile(rows, UTC_TZ, min_days=7)
    assert profile is not None
    assert profile.weekend[0] == 1.0
    assert profile.weekend[10] == 0.4  # filled from weekday


def test_dst_transition_day_still_48_slots() -> None:
    # Europe/London spring-forward (2026-03-29) has a 23-hour local day.
    rows = _history(date(2026, 3, 23), 14, 0.4, 0.8)
    profile = build_slot_profile(rows, LONDON, min_days=7)
    assert profile is not None
    assert len(profile.weekday) == SLOTS_PER_DAY
    assert len(profile.weekend) == SLOTS_PER_DAY
