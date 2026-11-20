"""The `health` Happy Hour row: five states, household-clock times, no secrets."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.health import Status, check_happy_hour
from tests.octopus_helpers import token_response

API = "http://backend.test/v1"
GRAPHQL = f"{API}/graphql/"
ECHO = "sk_test jwt-private A-1234ABCD"


def _settings(**kw: Any) -> Settings:
    base = dict(
        octopus_backend_api_url=API,
        octopus_api_key="sk_test",
        octopus_account_number="A-1234ABCD",
        timezone="Europe/London",
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _future_hour(hour: int) -> tuple[datetime, datetime]:
    day = (datetime.now(UTC) + timedelta(days=7)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return day, day + timedelta(hours=1)


def _event(event_id: Any, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "eventId": event_id,
        "eventType": "WEEKEND_HAPPY_HOUR",
        "startAt": start.isoformat(),
        "endAt": end.isoformat(),
    }


def _reply(events: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": {"savingSessions": {"account": {
        "joinedEvents": events,
    }}}})


@respx.mock
async def test_disabled_is_ok_and_sends_nothing() -> None:
    row = await check_happy_hour(_settings(happy_hour_enabled=False))
    assert (row.name, row.status, row.detail) == ("Happy Hour", Status.OK, "disabled")
    assert not respx.calls


async def test_not_configured_names_the_missing_setting_kind() -> None:
    # Default settings: no Octopus credentials (the backend URL has its default).
    row = await check_happy_hour(Settings())
    assert row.status is Status.OK
    assert "not configured" in row.detail
    for kind in ("Octopus API key", "Octopus account number"):
        assert kind in row.detail


@pytest.mark.parametrize("kw, kind", [
    ({"octopus_backend_api_url": ""}, "Octopus backend endpoint"),
    ({"octopus_api_key": ""}, "Octopus API key"),
    ({"octopus_account_number": ""}, "Octopus account number"),
])
@respx.mock
async def test_not_configured_names_only_what_is_missing(kw: dict[str, Any], kind: str) -> None:
    row = await check_happy_hour(_settings(**kw))
    assert row.status is Status.OK
    assert "not configured" in row.detail
    assert kind in row.detail
    assert not respx.calls


@respx.mock
async def test_read_failed_warns_and_leaks_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    respx.post(GRAPHQL).mock(side_effect=[
        token_response(), httpx.Response(500, text=ECHO + " http://backend.test"),
    ])
    row = await check_happy_hour(_settings())
    assert row.name == "Happy Hour"
    assert row.status is Status.WARN
    assert row.detail.endswith("no Happy Hour is planned")
    # The detail holds no secret and no endpoint; httpx itself logs request
    # URLs at INFO, so the log is swept for credentials only.
    secrets = ("sk_test", "jwt-private", "A-1234ABCD")
    for secret in secrets:
        assert secret not in row.detail
        assert secret not in caplog.text
    assert "backend.test" not in row.detail


@respx.mock
async def test_no_booking_is_ok() -> None:
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([])])
    row = await check_happy_hour(_settings())
    assert row.status is Status.OK
    assert "no booking" in row.detail
    assert "8066" not in row.detail


@respx.mock
async def test_booked_lists_times_on_the_household_clock() -> None:
    first_start, first_end = _future_hour(11)
    second_start, second_end = _future_hour(12)
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([
        _event(8066, first_start, first_end),
        _event(8067, second_start, second_end),
    ])])
    row = await check_happy_hour(_settings())
    assert row.status is Status.OK
    tz = ZoneInfo("Europe/London")
    expected = (
        f"{first_start.astimezone(tz):%Y-%m-%d %H:%M}-{first_end.astimezone(tz):%H:%M}",
        f"{second_start.astimezone(tz):%Y-%m-%d %H:%M}-{second_end.astimezone(tz):%H:%M}",
    )
    for when in expected:
        assert when in row.detail
    assert "8066" not in row.detail  # event ids stay out of the detail


@respx.mock
async def test_run_health_runs_the_happy_hour_check(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate the new row from every other probe, as the other run_health tests do.
    from ha_spark import health

    for name in dir(health):
        if name.startswith("check_") and name != "check_happy_hour":
            stub = health.CheckResult(name, health.Status.OK, "")
            monkeypatch.setattr(health, name, AsyncMock(return_value=stub))
    monkeypatch.setattr(health, "check_entity_config", lambda _: health.CheckResult(
        "Stub", health.Status.OK, ""
    ))
    monkeypatch.setattr(health, "check_hold_release", lambda _: [])
    monkeypatch.setattr(health, "check_discharge_ceiling", lambda *_: [])
    monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
    start, end = _future_hour(11)
    respx.post(GRAPHQL).mock(side_effect=[token_response(), _reply([_event(8066, start, end)])])
    rows = await health.run_health(_settings(db_path=str(tmp_path / "db")))
    row = next(r for r in rows if r.name == "Happy Hour")
    assert row.status is Status.OK
    tz = ZoneInfo("Europe/London")
    assert f"{start.astimezone(tz):%Y-%m-%d %H:%M}-{end.astimezone(tz):%H:%M}" in row.detail
