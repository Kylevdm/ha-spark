"""Octopus session polling, retention and planner-input wiring (#692)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import FreeSession, LoadForecast
from ha_spark.energy.octopus import (
    FreeElectricityRead,
    FreeElectricitySession,
    HappyHourBooking,
    HappyHourReading,
)
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.sources import SessionCache, SessionReadings, SessionSourceReading
from ha_spark.ha.rest import HomeAssistantRest
from tests.octopus_helpers import token_response
from tests.solar_source_helpers import entities_source

HA = "http://ha.test/api"
LONDON = ZoneInfo("Europe/London")
# A daytime plan instant: 12:00 BST (11:00 UTC).
NOW = datetime(2026, 6, 8, 12, 0, tzinfo=LONDON)
START = datetime(2026, 6, 8, 11, tzinfo=UTC)


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        ha_url="http://ha.test",
        ha_token="t",
        octopus_api_url="http://octo.test/v1",
        octopus_api_key="sk_test",
        octopus_account_number="A-1234ABCD",
        octopus_mpan="mpan-test",
        octopus_backend_api_url="http://backend.test/v1",
    )
    base.update(kw)
    return Settings(**base)


def _frozen_clock(frozen: datetime) -> type[datetime]:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Clock:
            at = frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)
            return cls.fromtimestamp(at.timestamp(), at.tzinfo)

    return _Clock


def _state(eid: str, state: str, attrs: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "entity_id": eid,
        "state": state,
        "attributes": attrs or {},
        "last_reported": datetime.now(UTC).isoformat(),
    })


def _free_payload(start: datetime, end: datetime, code: str = "fe-1") -> dict[str, Any]:
    return {"data": {
        "isEnrolledInCustomerFlexibilityCampaign": True,
        "customerFlexibilityCampaignEvents": {"edges": [{"node": {
            "code": code, "startAt": start.isoformat(), "endAt": end.isoformat(),
        }}]},
    }}


def _happy_payload(start: datetime, end: datetime, event_id: int = 12345) -> dict[str, Any]:
    return {"data": {"savingSessions": {"account": {"joinedEvents": [{
        "eventId": event_id,
        "eventType": "WEEKEND_HAPPY_HOUR",
        "startAt": start.isoformat(),
        "endAt": end.isoformat(),
    }]}}}}


def _sessions(*sessions: FreeSession) -> tuple[FreeElectricitySession, ...]:
    return tuple(
        FreeElectricitySession(s.session_id, s.start, s.end) for s in sessions
    )


def _reading(sessions: tuple[FreeElectricitySession, ...], now: datetime) -> FreeElectricityRead:
    state = "sessions" if sessions else "no_session"
    return FreeElectricityRead(state, now, sessions=sessions)


async def test_each_source_is_polled_once_per_half_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free_calls: list[datetime | None] = []
    happy_calls: list[datetime | None] = []

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        free_calls.append(now)
        return _reading((), now or START)

    async def fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        happy_calls.append(now)
        return HappyHourReading(now or START, ())

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", fake_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])
    for minute in range(24 * 60 + 1):  # a day of one-minute ticks
        clock[0] = START + timedelta(minutes=minute)
        await cache.poll(_settings())
    assert len(free_calls) == 49  # 24 h / 30 min, plus the first read
    assert len(happy_calls) == 49


async def test_a_failed_poll_waits_for_the_next_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free_calls = 0
    happy_calls = 0

    async def failing_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        nonlocal free_calls
        free_calls += 1
        raise httpx.ReadTimeout("Octopus is down")

    async def failing_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        nonlocal happy_calls
        happy_calls += 1
        raise httpx.ReadTimeout("Octopus is down")

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", failing_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", failing_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])
    for minute in range(121):
        clock[0] = START + timedelta(minutes=minute)
        readings = await cache.poll(_settings())
        assert readings.free_electricity.trusted is False
        assert readings.happy_hour.trusted is False
    assert free_calls == 5  # no retry storm
    assert happy_calls == 5


async def test_stale_reading_before_start_yields_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = FreeSession(
        "free_electricity", "future", START + timedelta(hours=3), START + timedelta(hours=4),
    )
    calls = [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        calls[0] += 1
        if calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        return _reading(_sessions(future), now or START)

    async def failed_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        return HappyHourReading(now or START, None)

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", failed_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])

    first = await cache.poll(_settings())
    assert [s.session_id for s in first.free_electricity.sessions] == ["future"]
    assert first.free_electricity.trusted is True

    # The last good reading is now >2 h old and the session has not started.
    clock[0] = START + timedelta(hours=2, minutes=1)
    stale = await cache.poll(_settings())
    assert stale.free_electricity.sessions == ()
    assert stale.free_electricity.trusted is False


async def test_stale_reading_keeps_a_started_session_to_its_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = FreeSession(
        "free_electricity", "started", START + timedelta(hours=1), START + timedelta(hours=6),
    )
    calls = [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        calls[0] += 1
        if calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        return _reading(_sessions(started), now or START)

    async def failed_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        return HappyHourReading(now or START, None)

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", failed_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])
    await cache.poll(_settings())

    # The reading is stale (>2 h) but the session has started, so it stays.
    clock[0] = START + timedelta(hours=2, minutes=1)
    kept = await cache.poll(_settings())
    assert [s.session_id for s in kept.free_electricity.sessions] == ["started"]
    assert kept.free_electricity.trusted is False

    clock[0] = START + timedelta(hours=6, minutes=31)
    ended = await cache.poll(_settings())
    assert ended.free_electricity.sessions == ()


@pytest.mark.parametrize("source", ["free_electricity", "happy_hour"])
async def test_stale_reading_cannot_resurrect_a_session_at_its_start(
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """A booking starting after last_good_read + 2 h never becomes eligible."""
    start = START + timedelta(hours=3)
    session = FreeSession(source, "late", start, start + timedelta(hours=1))
    free_calls, happy_calls = [0], [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        free_calls[0] += 1
        if free_calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        sessions = _sessions(session) if source == "free_electricity" else ()
        return _reading(sessions, now or START)

    async def fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        happy_calls[0] += 1
        if happy_calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        bookings = (
            (HappyHourBooking(session.session_id, session.start, session.end),)
            if source == "happy_hour" else ()
        )
        return HappyHourReading(now or START, bookings)

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", fake_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])

    first = getattr(await cache.poll(_settings()), source)
    assert [s.session_id for s in first.sessions] == ["late"]

    # The last good read expired before the session started. Time passing to
    # (and past) its start must not bring the booking back.
    for at in (start, start + timedelta(minutes=1)):
        clock[0] = at
        result = getattr(await cache.poll(_settings()), source)
        assert result.sessions == ()
        assert result.trusted is False


@pytest.mark.parametrize("source", ["free_electricity", "happy_hour"])
async def test_reading_exactly_two_hours_before_start_fails_closed(
    monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    start = START + timedelta(hours=2)
    session = FreeSession(source, "boundary", start, start + timedelta(hours=1))
    free_calls, happy_calls = [0], [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        free_calls[0] += 1
        if free_calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        sessions = _sessions(session) if source == "free_electricity" else ()
        return _reading(sessions, now or START)

    async def fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        happy_calls[0] += 1
        if happy_calls[0] > 1:
            raise httpx.ReadTimeout("Octopus is down")
        bookings = (
            (HappyHourBooking(session.session_id, session.start, session.end),)
            if source == "happy_hour" else ()
        )
        return HappyHourReading(now or START, bookings)

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", fake_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])
    await cache.poll(_settings())

    # Last good read is exactly two hours old as the session begins: fail closed.
    clock[0] = start
    result = getattr(await cache.poll(_settings()), source)
    assert result.sessions == ()
    assert result.trusted is False


async def test_sources_are_polled_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """One slow source must not add its timeout after the other's."""
    started: list[str] = []
    stalled: list[str] = []
    both_started = asyncio.Event()

    async def wait_for_peer(name: str) -> None:
        started.append(name)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.wait_for(both_started.wait(), timeout=0.5)
        except TimeoutError:
            stalled.append(name)

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        await wait_for_peer("free_electricity")
        return _reading((), now or START)

    async def fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        await wait_for_peer("happy_hour")
        return HappyHourReading(now or START, ())

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", fake_happy)
    readings = await SessionCache(clock=lambda: START).poll(_settings())

    assert set(started) == {"free_electricity", "happy_hour"}
    assert stalled == []
    assert readings == SessionReadings(
        SessionSourceReading((), True), SessionSourceReading((), True),
    )


async def test_failed_poll_keeps_previous_and_successful_empty_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = FreeSession(
        "free_electricity", "kept", START + timedelta(minutes=10), START + timedelta(hours=3),
    )
    calls = [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        calls[0] += 1
        if calls[0] == 1:
            return _reading(_sessions(kept), now or START)
        if calls[0] == 2:
            raise httpx.ReadTimeout("Octopus is down")
        return _reading((), now or START)

    async def failed_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        return HappyHourReading(now or START, None)

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", failed_happy)
    clock = [START]
    cache = SessionCache(clock=lambda: clock[0])

    first = await cache.poll(_settings())
    assert [s.session_id for s in first.free_electricity.sessions] == ["kept"]

    clock[0] = START + timedelta(minutes=31)
    after_failure = await cache.poll(_settings())
    assert [s.session_id for s in after_failure.free_electricity.sessions] == ["kept"]
    assert after_failure.free_electricity.trusted is True

    clock[0] = START + timedelta(minutes=62)
    after_empty = await cache.poll(_settings())
    assert after_empty.free_electricity.sessions == ()
    assert after_empty.free_electricity.trusted is True


async def test_disabled_or_unconfigured_sources_are_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*_a: object, **_kw: object) -> object:
        raise AssertionError("a disabled or unconfigured source must not be read")

    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", forbidden)
    monkeypatch.setattr(sources, "fetch_happy_hours", forbidden)
    cache = SessionCache(clock=lambda: START)

    disabled = await cache.poll(_settings(
        free_electricity_enabled=False, happy_hour_enabled=False,
    ))
    assert disabled == SessionReadings(
        SessionSourceReading((), True), SessionSourceReading((), True),
    )

    unconfigured = await cache.poll(_settings(octopus_api_key="", octopus_account_number=""))
    assert unconfigured == SessionReadings(
        SessionSourceReading((), True), SessionSourceReading((), True),
    )


@respx.mock
async def test_mocked_octopus_sessions_reach_the_cache() -> None:
    respx.post("http://octo.test/v1/graphql/").mock(side_effect=[
        token_response(),
        httpx.Response(200, json=_free_payload(
            START + timedelta(minutes=30), START + timedelta(minutes=90),
        )),
    ])
    respx.post("http://backend.test/v1/graphql/").mock(side_effect=[
        token_response(),
        httpx.Response(200, json=_happy_payload(
            START + timedelta(hours=2), START + timedelta(hours=3),
        )),
    ])
    cache = SessionCache(clock=lambda: START)
    readings = await cache.poll(_settings())

    assert readings.free_electricity.trusted is True
    assert readings.happy_hour.trusted is True
    assert {(s.kind, s.session_id) for s in readings.sessions} == {
        ("free_electricity", "fe-1"), ("happy_hour", "12345"),
    }


@respx.mock
async def test_mocked_free_electricity_produces_a_plan_purchase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{HA}/states/sensor.soc").mock(return_value=_state("sensor.soc", "20"))
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))
    respx.post("http://octo.test/v1/graphql/").mock(side_effect=[
        token_response(),
        httpx.Response(200, json=_free_payload(START, START + timedelta(hours=2))),
    ])
    settings = _settings(
        happy_hour_enabled=False, soc_entity="sensor.soc", db_path=str(tmp_path / "t.db"),
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest, source=entities_source(settings))

    assert run.inputs.free_electricity_trusted is True
    assert run.inputs.happy_hour_trusted is True  # deliberately off: known "none"
    assert [s.kind for s in run.inputs.free_sessions] == ["free_electricity"]
    purchase = next(
        p for p in run.plan.daytime_purchases if p.overlay == "free_electricity"
    )
    assert purchase.energy_kwh > 0
    assert "Free Electricity" in purchase.reason


@respx.mock
async def test_mocked_happy_hour_reaches_planner_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A booked Happy Hour is trusted planner input, not just a cached reading."""
    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{HA}/states/sensor.soc").mock(return_value=_state("sensor.soc", "20"))
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))
    respx.post("http://backend.test/v1/graphql/").mock(side_effect=[
        token_response(),
        httpx.Response(200, json=_happy_payload(
            START + timedelta(hours=2), START + timedelta(hours=3),
        )),
    ])
    settings = _settings(
        free_electricity_enabled=False, soc_entity="sensor.soc",
        db_path=str(tmp_path / "t.db"),
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest, source=entities_source(settings))

    assert run.inputs.free_electricity_trusted is True  # deliberately off: known "none"
    assert run.inputs.happy_hour_trusted is True
    assert [(s.kind, s.session_id) for s in run.inputs.free_sessions] == [
        ("happy_hour", "12345")
    ]


@respx.mock
async def test_reader_failure_does_not_fail_the_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    async def failing_reader(*_a: object, **_kw: object) -> object:
        raise httpx.ReadTimeout("Octopus is down")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", failing_reader)
    monkeypatch.setattr(sources, "fetch_happy_hours", failing_reader)
    respx.get(f"{HA}/states/sensor.soc").mock(return_value=_state("sensor.soc", "20"))
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))
    settings = _settings(
        happy_hour_enabled=False, soc_entity="sensor.soc", db_path=str(tmp_path / "t.db"),
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(settings, rest, source=entities_source(settings))

    assert run.inputs.free_sessions == ()
    assert run.inputs.free_electricity_trusted is False
    assert run.inputs.happy_hour_trusted is True  # disabled, not unreadable
    assert run.plan.daytime_purchases == ()


@respx.mock
async def test_current_plan_uses_the_daemons_shared_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every in-process plan surface reuses the daemon's half-hourly cache."""
    from ha_spark.devices.execution import execution_context

    monkeypatch.setattr(sources, "datetime", _frozen_clock(NOW))

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    free_calls, happy_calls = [0], [0]

    async def fake_free(_s: Settings, *, now: datetime | None = None) -> FreeElectricityRead:
        free_calls[0] += 1
        return _reading((), now or NOW)

    async def fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
        happy_calls[0] += 1
        return HappyHourReading(now or NOW, ())

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", fake_free)
    monkeypatch.setattr(sources, "fetch_happy_hours", fake_happy)
    respx.get(f"{HA}/states/sensor.soc").mock(return_value=_state("sensor.soc", "20"))
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))
    settings = _settings(soc_entity="sensor.soc", db_path=str(tmp_path / "t.db"))
    execution_context().session_cache = SessionCache()

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        for _ in range(2):
            run = await current_plan(settings, rest, source=entities_source(settings))
            assert run.inputs.free_electricity_trusted is True
            assert run.inputs.happy_hour_trusted is True

    assert free_calls == [1]
    assert happy_calls == [1]
