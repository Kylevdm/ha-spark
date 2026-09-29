"""Inverter-clock read and check against the household clock (#161)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_spark.devices.inverters.solis_clock import clock_error, clock_refusal, clock_registers
from ha_spark.ha.models import EntityState

LONDON = ZoneInfo("Europe/London")
# 14:03:00 BST on 28 Sep 2026.
READ_AT = datetime(2026, 9, 28, 13, 3, 0, tzinfo=UTC)
TOLERANCE = timedelta(minutes=5)


def _clock(value: str, *, last_updated: datetime | None = READ_AT) -> EntityState:
    return EntityState(
        entity_id="sensor.solis_control_inverter_clock",
        state=value,
        last_updated=last_updated,
    )


def test_error_is_the_inverter_face_minus_the_household_face_at_the_read() -> None:
    assert clock_error(_clock("26,9,28,14,3,35"), LONDON) == timedelta(seconds=35)


def test_an_inverter_left_on_gmt_reads_an_hour_behind_in_bst() -> None:
    assert clock_error(_clock("26,9,28,13,3,0"), LONDON) == timedelta(hours=-1)


@pytest.mark.parametrize(
    "value",
    [
        "unavailable",
        "unknown",
        "",
        "26,9,28,14,3",  # five fields
        "26,9,28,14,3,0,0",  # seven fields
        "26,13,28,14,3,0",  # month out of range
        "26,2,30,14,3,0",  # no 30 Feb
        "26,9,28,24,3,0",  # hour out of range
        "26,9,28,14,60,0",  # minute out of range
        "26,9,28,14,3,60",  # second out of range
        "100,9,28,14,3,0",  # year is two digits
        "26,9,28,14,3.5,0",  # not an integer
        "26,9,28,14,x,0",
    ],
)
def test_a_missing_or_out_of_range_field_is_unreadable(value: str) -> None:
    assert clock_error(_clock(value), LONDON) is None


def test_a_reading_without_last_updated_is_unreadable() -> None:
    assert clock_error(_clock("26,9,28,14,3,0", last_updated=None), LONDON) is None


def test_a_fresh_reading_within_tolerance_is_not_refused() -> None:
    now = READ_AT + timedelta(seconds=60)
    assert clock_refusal(_clock("26,9,28,14,7,59"), now, LONDON, TOLERANCE) is None


def test_an_error_above_tolerance_is_refused_with_its_size() -> None:
    reason = clock_refusal(_clock("26,9,28,13,3,0"), READ_AT, LONDON, TOLERANCE)
    assert reason is not None
    assert "60 min behind" in reason
    assert "5 min tolerance" in reason
    assert "sync-clock" in reason


def test_an_error_ahead_is_described_as_ahead() -> None:
    reason = clock_refusal(_clock("26,9,28,14,9,0"), READ_AT, LONDON, TOLERANCE)
    assert reason is not None
    assert "6 min ahead" in reason


def test_a_reading_older_than_sixty_seconds_is_refused() -> None:
    now = READ_AT + timedelta(seconds=61)
    reason = clock_refusal(_clock("26,9,28,14,3,0"), now, LONDON, TOLERANCE)
    assert reason is not None
    assert "61 s old" in reason


def test_an_unreadable_clock_is_refused() -> None:
    reason = clock_refusal(_clock("unavailable"), READ_AT, LONDON, TOLERANCE)
    assert reason == "inverter clock unreadable"


def test_a_missing_entity_is_refused_as_unreadable() -> None:
    assert clock_refusal(None, READ_AT, LONDON, TOLERANCE) == "inverter clock unreadable"


def test_sync_registers_are_the_household_wall_clock_with_a_two_digit_year() -> None:
    assert clock_registers(READ_AT, LONDON) == [26, 9, 28, 14, 3, 0]


def test_sync_registers_round_the_seconds_down() -> None:
    assert clock_registers(READ_AT + timedelta(seconds=7, microseconds=900_000), LONDON) == [
        26,
        9,
        28,
        14,
        3,
        7,
    ]
