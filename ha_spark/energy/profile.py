"""Slot-of-day load profile: median kWh per local half-hour slot, weekday/weekend.

Pure functions — the deterministic v2 load model, and the baseline the ML
model (ml.py) must beat in `forecast-eval` before `load_model: auto` uses it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from statistics import median
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval, SlotProfile


def _slot_index(hour: int, minute: int) -> int:
    return hour * 2 + minute // 30


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def history_coverage(
    intervals: Sequence[ConsumptionInterval], tz: ZoneInfo
) -> tuple[int, int]:
    """(distinct local dates, weekend dates) covered by ``intervals``."""
    dates = {interval.start.astimezone(tz).date() for interval in intervals}
    return len(dates), sum(1 for d in dates if _is_weekend(d))


def build_slot_profile(
    intervals: Sequence[ConsumptionInterval], tz: ZoneInfo, *, min_days: int
) -> SlotProfile | None:
    """Bucket readings by (local slot, weekday|weekend) and take medians.

    Returns None when history is too thin (< ``min_days`` distinct local dates,
    or fewer than 2 weekend dates) — callers then fall back to the v1 forecast.
    """
    buckets: dict[tuple[bool, int], list[float]] = {}
    dates: set[date] = set()
    for interval in intervals:
        local = interval.start.astimezone(tz)
        d = local.date()
        dates.add(d)
        buckets.setdefault((_is_weekend(d), _slot_index(local.hour, local.minute)), []).append(
            interval.kwh
        )

    weekend_dates = sum(1 for d in dates if _is_weekend(d))
    if len(dates) < min_days or weekend_dates < 2:
        return None

    all_values = [v for vs in buckets.values() for v in vs]
    overall_mean = sum(all_values) / len(all_values) if all_values else 0.0

    def slot_value(weekend: bool, slot: int) -> float:
        values = buckets.get((weekend, slot))
        if values:
            return float(median(values))
        # Hole-filling: same slot of the other day-type, else the overall mean —
        # a published profile never has gaps.
        other = buckets.get((not weekend, slot))
        if other:
            return float(median(other))
        return overall_mean

    return SlotProfile(
        weekday=tuple(slot_value(False, s) for s in range(SLOTS_PER_DAY)),
        weekend=tuple(slot_value(True, s) for s in range(SLOTS_PER_DAY)),
        days_used=len(dates),
    )


def predict_day_slots(profile: SlotProfile, day: date) -> tuple[float, ...]:
    """The predicted per-slot load for ``day`` (slot-of-day order)."""
    return profile.weekend if _is_weekend(day) else profile.weekday
