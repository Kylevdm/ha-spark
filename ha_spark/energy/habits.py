"""Learned household habits from the recorded signal/forecast ledger (Phase 6E).

Pure functions over data the earlier phases already accumulate:

- ``predict_occupancy`` — tomorrow's occupancy fraction from the day-type
  pattern of recorded ``occupancy_home_frac`` samples (6A). Fed into the ML
  feature builder (6B) so the model has a real occupancy value for the target
  day instead of the history mean.
- ``learn_away_factor`` — the empirical load multiplier on past ``away``
  context periods (6C/6D) versus same-day-type normal days. Used in place of
  the configured ``away_load_factor`` once enough away history exists.
- ``predict_actions`` — the seed of the usernotes ``predict_actions(context)``
  habit API: advisory ``(action, confidence, reason)`` tuples the orchestrator
  logs or (later) executes, gated by ``PROACTIVE_MODE``. It never acts itself.

No ML dependency — deterministic medians keep it explainable and lets it run
without the ``[habits]`` extra.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

from ha_spark.energy.ml import UK_BANK_HOLIDAYS

# Minimums below which a learned value is too thin to trust (caller falls back).
MIN_OCCUPANCY_DAYS_PER_TYPE = 2
MIN_AWAY_SAMPLES = 3


def is_holiday_like(d: date) -> bool:
    """A day that behaves like a weekend for occupancy/load: weekend or UK bank holiday."""
    return d.weekday() >= 5 or d in UK_BANK_HOLIDAYS


def _daily_means(
    samples: Sequence[tuple[datetime, float]], tz: ZoneInfo
) -> dict[date, float]:
    by_date: dict[date, list[float]] = {}
    for ts, value in samples:
        by_date.setdefault(ts.astimezone(tz).date(), []).append(value)
    return {d: sum(vs) / len(vs) for d, vs in by_date.items()}


def predict_occupancy(
    samples: Sequence[tuple[datetime, float]], target_date: date, tz: ZoneInfo
) -> float | None:
    """Predict ``target_date``'s occupancy fraction from same-day-type history.

    Returns the median of recorded daily-mean occupancy for days of the same
    type (weekday vs weekend/holiday); falls back to all days, then to None
    when history is too thin to be meaningful.
    """
    daily = _daily_means(samples, tz)
    if not daily:
        return None
    target_type = is_holiday_like(target_date)
    same_type = [m for d, m in daily.items() if is_holiday_like(d) == target_type]
    if len(same_type) < MIN_OCCUPANCY_DAYS_PER_TYPE:
        # Too few of this day-type; use the whole history if it's substantial.
        if len(daily) < MIN_OCCUPANCY_DAYS_PER_TYPE:
            return None
        same_type = list(daily.values())
    return float(median(same_type))


def learn_away_factor(
    daily_actuals: Mapping[date, float], away_dates: set[date]
) -> tuple[float | None, int]:
    """Learn the away load multiplier: away-day load / same-type normal-day load.

    Returns ``(factor, n)`` where ``n`` is the number of away days that had
    both an actual and a same-day-type normal baseline. ``factor`` is None
    when ``n`` is below :data:`MIN_AWAY_SAMPLES` (caller keeps the configured
    default).
    """
    normal: dict[bool, list[float]] = {True: [], False: []}
    for d, kwh in daily_actuals.items():
        if d not in away_dates:
            normal[is_holiday_like(d)].append(kwh)

    ratios: list[float] = []
    for d, kwh in daily_actuals.items():
        if d not in away_dates:
            continue
        baseline = normal[is_holiday_like(d)]
        if not baseline:
            continue
        base = median(baseline)
        if base > 0:
            ratios.append(kwh / base)
    if len(ratios) < MIN_AWAY_SAMPLES:
        return None, len(ratios)
    return float(median(ratios)), len(ratios)


@dataclass(frozen=True)
class HabitContext:
    """The inputs ``predict_actions`` reasons over for a target day."""

    target_date: date
    predicted_occupancy: float | None
    away_active: bool
    learned_away_factor: float | None


@dataclass(frozen=True)
class PredictedAction:
    """One advisory action: what, how sure, and why. Never executed here."""

    action: str
    confidence: float
    reason: str


# Below this predicted occupancy fraction we'd expect a materially emptier home.
_LOW_OCCUPANCY = 0.2


def predict_actions(ctx: HabitContext) -> list[PredictedAction]:
    """Advisory predictions for ``ctx`` — the seed of the habit API.

    The orchestrator requests these every run and logs/executes them per
    ``PROACTIVE_MODE``; this function only reasons, it never acts.
    """
    actions: list[PredictedAction] = []
    occ = ctx.predicted_occupancy
    if occ is not None and occ < _LOW_OCCUPANCY and not ctx.away_active:
        actions.append(
            PredictedAction(
                action="suggest_away_context",
                confidence=round(1.0 - occ / _LOW_OCCUPANCY, 2),
                reason=(
                    f"occupancy for {ctx.target_date:%a %d %b} is predicted low "
                    f"(~{occ * 100:.0f}% home) but no away context is set"
                ),
            )
        )
    if ctx.away_active:
        factor = ctx.learned_away_factor
        detail = (
            f"learned away load is ~{factor * 100:.0f}% of normal"
            if factor is not None
            else "using the configured away factor"
        )
        actions.append(
            PredictedAction(
                action="reduce_overnight_charge",
                confidence=0.9 if factor is not None else 0.6,
                reason=f"an away period is active; {detail}",
            )
        )
    return actions
