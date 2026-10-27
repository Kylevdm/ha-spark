"""Native forecast validation stays local to the heartbeat field."""

from datetime import UTC, datetime

import pytest

from ha_spark.energy.solar_source import parse_native_forecast

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
VALID = {"status": "ok", "fetched_at": NOW.isoformat(), "calls_used": 2,
         "calls_limit": 10, "slots": [[NOW.isoformat(), 1, 2, 3]]}


def test_native_forecast_valid_and_unconfigured():
    result = parse_native_forecast(VALID, received_at=NOW)
    assert result.kind == "native"
    assert result.forecast is not None
    assert result.forecast.slots == ((NOW, 1, 2, 3),)
    assert parse_native_forecast(None, received_at=NOW).kind == "entities"


def test_native_forecast_accepts_precision_and_five_second_skew():
    result = parse_native_forecast(VALID | {
        "fetched_at": "2026-10-24T12:00:05Z",
        "slots": [[NOW.isoformat(), 1.123456, 2.123456, 3.123456]],
    }, received_at=NOW)
    assert result.forecast is not None
    assert result.forecast.slots[0][1] == 1.123456


def test_native_forecast_configured_without_data():
    result = parse_native_forecast(VALID | {
        "status": "no_data", "fetched_at": None, "slots": [],
    }, received_at=NOW)
    assert result.kind == "native"
    assert result.forecast is not None and result.forecast.slots == ()


@pytest.mark.parametrize("updates", [
    {"status": "secret"}, {"calls_used": True}, {"calls_used": -1}, {"calls_used": 1001},
    {"calls_limit": 11}, {"calls_limit": True}, {"calls_used": 1.0},
    {"fetched_at": "2026-10-24T12:00:06Z"}, {"slots": [[NOW.isoformat(), 3, 2, 1]]},
    {"slots": [[NOW.isoformat(), 0, 1, 101]]}, {"slots": [[NOW.isoformat(), 0, 1, True]]},
    {"slots": [["2026-10-24T12:01:00Z", 0, 1, 2]]},
    {"slots": [[NOW.isoformat(), 0, 1, 2]] * 2},
    {"slots": [[NOW.isoformat(), 0, 1, float("nan")]]},
    {"slots": [[NOW.isoformat(), 0, 1, float("inf")]]},
    {"slots": [[NOW.isoformat(), 0, 1, 2]] * 121},
    {"slots": [[NOW.isoformat(), 0, 1, 2], ["2026-10-24T13:00:00Z", 0, 1, 2]]},
    {"fetched_at": "2026-10-24T12:00:00"},
    {"fetched_at": None},
    {"fetched_at": 123},
    {"slots": [[NOW.isoformat(), -1, 1, 2]]},
])
def test_native_forecast_unusable(updates):
    result = parse_native_forecast(VALID | updates, received_at=NOW)
    assert result.kind == "native"
    assert result.forecast is None
