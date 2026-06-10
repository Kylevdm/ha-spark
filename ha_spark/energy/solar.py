"""Distribute a solar-tomorrow total (kWh) across the day's half-hour slots."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY

# Fallback daylight window (local clock) when no detailed forecast is available.
_DAYLIGHT_START_SLOT = 16  # 08:00
_DAYLIGHT_END_SLOT = 36  # 18:00


def _fallback_shape() -> list[float]:
    """Half-sine over the daylight window — a crude but adequate solar curve."""
    n = _DAYLIGHT_END_SLOT - _DAYLIGHT_START_SLOT
    shape = [0.0] * SLOTS_PER_DAY
    for i in range(n):
        shape[_DAYLIGHT_START_SLOT + i] = math.sin(math.pi * (i + 0.5) / n)
    return shape


def distribute_solar(
    total_kwh: float,
    detailed: Sequence[tuple[datetime, float]] | None,
    tz: ZoneInfo,
    day: date,
) -> tuple[float, ...]:
    """Return 48 slot-of-day kWh values for ``day`` summing to ``total_kwh``.

    A Solcast ``detailedForecast`` (period_start, pv_estimate) is used only as
    relative *weights* scaled to the entity's state total, which sidesteps the
    kW-vs-kWh ambiguity across integration versions.
    """
    if total_kwh <= 0:
        return (0.0,) * SLOTS_PER_DAY

    weights = [0.0] * SLOTS_PER_DAY
    if detailed:
        for period_start, value in detailed:
            local = period_start.astimezone(tz) if period_start.tzinfo else period_start
            if local.date() != day or value <= 0:
                continue
            weights[local.hour * 2 + local.minute // 30] += value
    if sum(weights) <= 0:
        weights = _fallback_shape()

    scale = total_kwh / sum(weights)
    return tuple(w * scale for w in weights)
