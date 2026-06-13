"""Weather-aware ML slot load model (Phase 6B) — optional, import-guarded.

Gradient-boosted quantile regression over local half-hour slots. The package
works without the ``[habits]`` extra: ``ml_available()`` gates every caller,
and a missing scikit-learn simply means the forecast falls back to the median
slot profile.

Per-slot features: slot index, day-of-week, month (season), heating degree
hours ``max(0, 15.5 − temp_c)`` (captures heat-pump demand implicitly — the
HP is metered inside the house-load history), 7-day same-slot lag median,
previous-day total, occupancy fraction, and a UK bank-holiday flag.

The model is refit on every plan run (sub-second on ~3k rows) — stateless and
auditable, no persisted model files. P50 drives the plan; P90 feeds the
quantile buffer (``buffer_mode: quantile``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY, ConsumptionInterval

try:
    from sklearn.ensemble import HistGradientBoostingRegressor

    _HAS_ML = True
except ImportError:  # pragma: no cover - exercised only without the [habits] extra
    _HAS_ML = False


def ml_available() -> bool:
    """True when the optional ML dependencies ([habits] extra) are importable."""
    return _HAS_ML


# Heating-degree base: demand below this outdoor temperature scales heating load.
HDD_BASE_C = 15.5

# A training day must have most of its 48 slots present to be used.
_MIN_SLOTS_PER_DAY = 40

# Tomorrow needs temperature forecast coverage for at least half its hours.
_MIN_TARGET_TEMP_HOURS = 12

# England & Wales bank holidays (treated like weekends by the model).
UK_BANK_HOLIDAYS = frozenset(
    {
        date(2025, 1, 1), date(2025, 4, 18), date(2025, 4, 21), date(2025, 5, 5),
        date(2025, 5, 26), date(2025, 8, 25), date(2025, 12, 25), date(2025, 12, 26),
        date(2026, 1, 1), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 4),
        date(2026, 5, 25), date(2026, 8, 31), date(2026, 12, 25), date(2026, 12, 28),
        date(2027, 1, 1), date(2027, 3, 26), date(2027, 3, 29), date(2027, 5, 3),
        date(2027, 5, 31), date(2027, 8, 30), date(2027, 12, 27), date(2027, 12, 28),
    }
)


@dataclass(frozen=True)
class MLPrediction:
    """Per-slot quantile load prediction for one target day."""

    p50: tuple[float, ...]  # 48 slot kWh, the planning forecast
    p90: tuple[float, ...]  # 48 slot kWh, upper quantile (>= p50 elementwise)
    days_used: int


def _slot_days(
    intervals: Sequence[ConsumptionInterval], tz: ZoneInfo
) -> dict[date, list[float]]:
    """Bucket intervals into complete-ish local days of 48 slot values.

    Days missing more than ``48 - _MIN_SLOTS_PER_DAY`` slots are dropped;
    remaining holes are filled with the day's mean so every kept day is dense.
    """
    raw: dict[date, list[float | None]] = {}
    for iv in intervals:
        local = iv.start.astimezone(tz)
        slots = raw.setdefault(local.date(), [None] * SLOTS_PER_DAY)
        slots[local.hour * 2 + local.minute // 30] = iv.kwh
    days: dict[date, list[float]] = {}
    for d, slots in raw.items():
        present = [v for v in slots if v is not None]
        if len(present) < _MIN_SLOTS_PER_DAY:
            continue
        fill = sum(present) / len(present)
        days[d] = [v if v is not None else fill for v in slots]
    return days


def _slot_temp(
    d: date, slot: int, temps: Mapping[datetime, float], tz: ZoneInfo
) -> float | None:
    """Outdoor temp at the slot's local start, looked up by UTC hour."""
    local = datetime(d.year, d.month, d.day, slot // 2, (slot % 2) * 30, tzinfo=tz)
    hour_utc = local.astimezone(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
    return temps.get(hour_utc)


def _features(
    d: date, slot: int, temp: float, lag_med: float, prev_total: float, occ: float
) -> list[float]:
    return [
        float(slot),
        float(d.weekday()),
        float(d.month),
        max(0.0, HDD_BASE_C - temp),
        lag_med,
        prev_total,
        occ,
        1.0 if d in UK_BANK_HOLIDAYS else 0.0,
    ]


def train_and_predict(
    intervals: Sequence[ConsumptionInterval],
    temps: Mapping[datetime, float],
    occupancy_by_date: Mapping[date, float],
    target_date: date,
    tz: ZoneInfo,
    *,
    min_days: int = 14,
) -> MLPrediction | None:
    """Fit P50/P90 quantile models on the history and predict ``target_date``.

    Returns None whenever the model cannot responsibly run: no sklearn, no
    temperatures, fewer than ``min_days`` usable training days, or no
    temperature forecast for the target day — callers fall back to the median.
    """
    if not _HAS_ML or not temps:
        return None
    days = _slot_days(intervals, tz)
    ordered = sorted(days)
    # The first day only provides lag features, so it is not a training row.
    if len(ordered) - 1 < min_days:
        return None
    temp_fallback = sum(temps.values()) / len(temps)
    occ_values = list(occupancy_by_date.values())
    occ_fallback = sum(occ_values) / len(occ_values) if occ_values else 0.5

    target_temps_found = sum(
        1
        for slot in range(0, SLOTS_PER_DAY, 2)
        if _slot_temp(target_date, slot, temps, tz) is not None
    )
    if target_temps_found < _MIN_TARGET_TEMP_HOURS:
        return None

    def slot_temp(d: date, slot: int) -> float:
        temp = _slot_temp(d, slot, temps, tz)
        return temp if temp is not None else temp_fallback

    x_train: list[list[float]] = []
    y_train: list[float] = []
    for i in range(1, len(ordered)):
        d = ordered[i]
        lag_days = ordered[max(0, i - 7) : i]
        prev_total = sum(days[ordered[i - 1]])
        occ = occupancy_by_date.get(d, occ_fallback)
        for slot in range(SLOTS_PER_DAY):
            lag_med = float(median(days[ld][slot] for ld in lag_days))
            x_train.append(_features(d, slot, slot_temp(d, slot), lag_med, prev_total, occ))
            y_train.append(days[d][slot])

    lag_days = ordered[-7:]
    prev_total = sum(days[ordered[-1]])
    occ = occupancy_by_date.get(target_date, occ_fallback)
    x_target = [
        _features(
            target_date,
            slot,
            slot_temp(target_date, slot),
            float(median(days[ld][slot] for ld in lag_days)),
            prev_total,
            occ,
        )
        for slot in range(SLOTS_PER_DAY)
    ]

    def _fit_predict(quantile: float) -> list[float]:
        model = HistGradientBoostingRegressor(
            loss="quantile", quantile=quantile, random_state=0
        )
        model.fit(x_train, y_train)
        return [max(0.0, float(v)) for v in model.predict(x_target)]

    p50 = _fit_predict(0.5)
    p90 = [max(a, b) for a, b in zip(_fit_predict(0.9), p50, strict=True)]
    return MLPrediction(p50=tuple(p50), p90=tuple(p90), days_used=len(ordered) - 1)
