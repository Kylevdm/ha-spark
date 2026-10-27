"""The shared UTC instant formatter used by view-only stores."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ha_spark.energy.models import utc_iso

LONDON = ZoneInfo("Europe/London")


def test_utc_iso_converts_non_utc_timezone() -> None:
    at = datetime(2026, 6, 1, 14, 30, tzinfo=LONDON)
    assert utc_iso(at) == "2026-06-01T13:30:00+00:00"


def test_utc_iso_passes_through_utc() -> None:
    at = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
    assert utc_iso(at) == "2026-06-01T13:30:00+00:00"


def test_utc_iso_preserves_sub_second_resolution() -> None:
    at = datetime(2026, 6, 1, 13, 30, 0, 123000, tzinfo=UTC)
    assert utc_iso(at) == "2026-06-01T13:30:00.123000+00:00"
