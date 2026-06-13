"""Tests for the learned-habits module (occupancy, away factor, predict_actions)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ha_spark.energy.habits import (
    HabitContext,
    is_holiday_like,
    learn_away_factor,
    predict_actions,
    predict_occupancy,
)

_TZ = ZoneInfo("UTC")


def _samples(values_by_date: dict[date, float]) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for d, v in values_by_date.items():
        # two samples a day to exercise daily averaging
        out.append((datetime(d.year, d.month, d.day, 9, tzinfo=UTC), v))
        out.append((datetime(d.year, d.month, d.day, 18, tzinfo=UTC), v))
    return out


def test_is_holiday_like_weekend_and_bank_holiday() -> None:
    assert is_holiday_like(date(2026, 6, 13))  # Saturday
    assert is_holiday_like(date(2026, 12, 25))  # Christmas (bank holiday, a Friday)
    assert not is_holiday_like(date(2026, 6, 15))  # Monday


def test_predict_occupancy_uses_same_day_type_median() -> None:
    # Weekdays ~0.3 home, weekends ~0.9 home over two weeks.
    base = date(2026, 6, 1)  # Monday
    vals: dict[date, float] = {}
    for i in range(14):
        d = base + timedelta(days=i)
        vals[d] = 0.9 if is_holiday_like(d) else 0.3
    samples = _samples(vals)

    weekday_target = date(2026, 6, 17)  # Wednesday
    weekend_target = date(2026, 6, 20)  # Saturday
    assert predict_occupancy(samples, weekday_target, _TZ) == 0.3
    assert predict_occupancy(samples, weekend_target, _TZ) == 0.9


def test_predict_occupancy_none_without_history() -> None:
    assert predict_occupancy([], date(2026, 6, 17), _TZ) is None


def test_learn_away_factor_from_past_away_periods() -> None:
    # Normal weekdays = 20 kWh; the three away weekdays drew ~8 kWh -> ratio 0.4.
    base = date(2026, 6, 1)  # Monday
    actuals: dict[date, float] = {}
    away: set[date] = set()
    for i in range(21):
        d = base + timedelta(days=i)
        if is_holiday_like(d):
            continue
        actuals[d] = 20.0
    for d in (date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)):  # away weekdays
        actuals[d] = 8.0
        away.add(d)

    factor, n = learn_away_factor(actuals, away)
    assert n == 3
    assert factor == 0.4


def test_learn_away_factor_none_when_too_few_samples() -> None:
    actuals = {date(2026, 6, 1): 20.0, date(2026, 6, 2): 8.0}
    factor, n = learn_away_factor(actuals, {date(2026, 6, 2)})
    assert factor is None
    assert n == 1


def test_predict_actions_suggests_away_on_low_occupancy() -> None:
    ctx = HabitContext(
        target_date=date(2026, 6, 17),
        predicted_occupancy=0.05,
        away_active=False,
        learned_away_factor=None,
    )
    actions = predict_actions(ctx)
    assert [a.action for a in actions] == ["suggest_away_context"]
    assert actions[0].confidence > 0


def test_predict_actions_no_suggestion_when_away_already_set() -> None:
    ctx = HabitContext(
        target_date=date(2026, 6, 17),
        predicted_occupancy=0.05,
        away_active=True,
        learned_away_factor=0.4,
    )
    actions = predict_actions(ctx)
    assert [a.action for a in actions] == ["reduce_overnight_charge"]
    assert "40%" in actions[0].reason


def test_predict_actions_quiet_on_normal_day() -> None:
    ctx = HabitContext(
        target_date=date(2026, 6, 17),
        predicted_occupancy=0.6,
        away_active=False,
        learned_away_factor=None,
    )
    assert predict_actions(ctx) == []
