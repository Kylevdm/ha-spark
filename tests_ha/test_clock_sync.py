"""Household-clock face and zone-change parity, without HA or hardware."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_spark.clock_sync import clock_error, clock_registers, last_clock_change

ZONE = ZoneInfo("Europe/London")


@pytest.mark.parametrize(("instant", "change"), [
    ("2026-03-29T01:00:00+00:00", "2026-03-29T01:00:00+00:00"),
    ("2026-10-25T06:59:59+00:00", "2026-10-25T01:00:00+00:00"),
    ("2026-10-25T07:00:01+00:00", None),
    ("2026-10-24T12:00:00+00:00", None),
])
def test_clock_change_to_the_second(instant: str, change: str | None) -> None:
    now = datetime.fromisoformat(instant).astimezone(ZONE)
    assert last_clock_change(now) == (datetime.fromisoformat(change) if change else None)


def test_clock_face_not_elapsed_time() -> None:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    assert clock_error([26, 7, 1, 12, 0, 0], now, ZONE) == -timedelta(hours=1)
    assert clock_registers(now.replace(microsecond=123), ZONE) == [26, 7, 1, 13, 0, 0]


@pytest.mark.parametrize("words", [[], [26] * 5, [2026, 1, 1, 0, 0, 0],
                                     [26, 2, 30, 0, 0, 0], [26, 1, 1, 24, 0, 0]])
def test_unreadable_clock(words: list[int]) -> None:
    assert clock_error(words, datetime(2026, 1, 1, tzinfo=UTC), ZONE) is None


def test_naive_read_time_is_unreadable() -> None:
    assert clock_error([26, 1, 1, 0, 0, 0], datetime(2026, 1, 1), ZONE) is None
