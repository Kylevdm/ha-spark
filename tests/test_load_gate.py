"""MVP gate: derived base load materially improves the median forecast's error."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from ha_spark.energy.derived_base_load import ComponentSeries, derive_base_load
from ha_spark.energy.models import ConsumptionInterval
from ha_spark.energy.profile import build_slot_profile, predict_day_slots

TZ = ZoneInfo("UTC")
START = date(2026, 1, 1)
TRAINING = [START + timedelta(days=offset) for offset in range(28)]
HELD_OUT = [START + timedelta(days=28 + offset) for offset in range(14)]


def _base_hour(day: date, hour: int) -> float:
    weekend = day.weekday() >= 5
    if hour < 6:
        return 0.25 if weekend else 0.20
    if hour < 9:
        return 0.40 if weekend else 0.50
    if hour < 17:
        return 0.60 if weekend else 0.30
    if hour < 22:
        return 0.80 if weekend else 0.70
    return 0.40 if weekend else 0.30


def _charge_hour(day: date, hour: int) -> float:
    if hour >= 6:
        return 0.0
    # The battery charges overnight by a different amount each night, so a
    # profile trained on raw consumption learns a useless middle value.
    return ((day.day * 1.7) % 7) / 6


def _series(days: list[date]):
    grid_import: dict[datetime, float] = {}
    battery_charge: dict[datetime, float] = {}
    base: dict[datetime, float] = {}
    for day in days:
        for hour in range(24):
            instant = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            base[instant] = _base_hour(day, hour)
            battery_charge[instant] = _charge_hour(day, hour)
            grid_import[instant] = base[instant] + battery_charge[instant]
    return grid_import, battery_charge, base


def _intervals(hourly: dict[datetime, float]) -> list[ConsumptionInterval]:
    intervals = []
    for instant, kwh in sorted(hourly.items()):
        intervals.append(ConsumptionInterval(
            instant, instant + timedelta(minutes=30), kwh / 2,
        ))
        intervals.append(ConsumptionInterval(
            instant + timedelta(minutes=30), instant + timedelta(hours=1), kwh / 2,
        ))
    return intervals


def _median_mae(training: dict[datetime, float], actual: dict[datetime, float]) -> float:
    profile = build_slot_profile(_intervals(training), TZ, min_days=7)
    assert profile is not None
    errors = []
    for day in HELD_OUT:
        predicted = sum(predict_day_slots(profile, day))
        measured = sum(kwh for instant, kwh in actual.items() if instant.date() == day)
        errors.append(abs(predicted - measured))
    return sum(errors) / len(errors)


Derive = Callable[[dict[datetime, float], dict[datetime, float]], dict[datetime, float]]


def _derived(grid_import: dict[datetime, float], battery_charge: dict[datetime, float]):
    return dict(derive_base_load(
        ComponentSeries("sensor.grid_import", grid_import),
        battery_charge=ComponentSeries("sensor.battery_charge", battery_charge),
    ).hourly)


def _bypassed(grid_import: dict[datetime, float], battery_charge: dict[datetime, float]):
    return dict(grid_import)


def _gate(derive: Derive) -> tuple[float, float]:
    """(median MAE on the derived series, median MAE on the raw series)."""
    train_raw, train_charge, _ = _series(TRAINING)
    held_raw, held_charge, _ = _series(HELD_OUT)
    derived_error = _median_mae(
        derive(train_raw, train_charge), derive(held_raw, held_charge),
    )
    raw_error = _median_mae(train_raw, held_raw)
    return derived_error, raw_error


def test_derivation_removes_the_overnight_charging():
    raw, charge, base = _series(TRAINING)
    derived = _derived(raw, charge)
    assert derived.keys() == base.keys()
    assert all(abs(derived[instant] - base[instant]) < 1e-9 for instant in base)


def test_derived_base_load_reduces_median_error_by_at_least_25_percent():
    derived_error, raw_error = _gate(_derived)
    assert raw_error > 0
    assert derived_error <= 0.75 * raw_error


def test_gate_fails_when_the_derivation_is_bypassed():
    derived_error, raw_error = _gate(_bypassed)
    assert not derived_error <= 0.75 * raw_error
