"""Tests for solar slot distribution."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ha_spark.energy.models import SLOTS_PER_DAY
from ha_spark.energy.solar import distribute_solar

UTC_TZ = ZoneInfo("UTC")
DAY = date(2026, 6, 10)


def test_fallback_shape_sums_to_total_and_is_daylight_only() -> None:
    slots = distribute_solar(10.0, None, UTC_TZ, DAY)
    assert len(slots) == SLOTS_PER_DAY
    assert sum(slots) == pytest.approx(10.0)
    assert all(s == 0.0 for s in slots[:16])  # before 08:00
    assert all(s == 0.0 for s in slots[36:])  # after 18:00
    assert slots[26] > slots[17]  # midday beats early morning


def test_detailed_forecast_used_as_scaled_weights() -> None:
    detailed = [
        (datetime(2026, 6, 10, 10, 0, tzinfo=UTC_TZ), 1.0),
        (datetime(2026, 6, 10, 12, 0, tzinfo=UTC_TZ), 3.0),
        # Wrong day: ignored.
        (datetime(2026, 6, 11, 12, 0, tzinfo=UTC_TZ), 9.0),
    ]
    slots = distribute_solar(8.0, detailed, UTC_TZ, DAY)
    assert sum(slots) == pytest.approx(8.0)
    assert slots[20] == pytest.approx(2.0)  # 10:00 — weight 1 of 4
    assert slots[24] == pytest.approx(6.0)  # 12:00 — weight 3 of 4


def test_detailed_forecast_for_other_day_falls_back_to_shape() -> None:
    detailed = [(datetime(2026, 6, 11, 12, 0, tzinfo=UTC_TZ), 5.0)]
    slots = distribute_solar(4.0, detailed, UTC_TZ, DAY)
    assert sum(slots) == pytest.approx(4.0)
    assert slots[24] > 0  # fallback shape in use


def test_zero_total_is_all_zeros() -> None:
    assert distribute_solar(0.0, None, UTC_TZ, DAY) == (0.0,) * SLOTS_PER_DAY
