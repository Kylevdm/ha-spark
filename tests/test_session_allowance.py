"""Happy Hour import counter: persistence, fail-closed readings and health (#694)."""
from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import FreeSession, LoadForecast
from ha_spark.energy.octopus import HappyHourBooking, HappyHourReading
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.session_allowance import (
    GridImport,
    SessionAllowanceCounter,
    SessionAllowanceStore,
    read_grid_import,
)
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.sources import SessionCache
from ha_spark.energy.tariff import HAPPY_HOUR_ALLOWANCE_KWH
from ha_spark.ha.rest import HomeAssistantRest
from tests.solar_source_helpers import entities_source

HA = "http://ha.test/api"
LONDON = ZoneInfo("Europe/London")
START = datetime(2026, 6, 8, 12, tzinfo=LONDON)
END = START + timedelta(hours=1)
GRID = "sensor.grid_import"
MAX_JUMP_KW = 18.0


@pytest.mark.parametrize("age_seconds,usable", [(600, True), (601, False)])
@respx.mock
async def test_grid_import_freshness_uses_ten_minutes(
    tmp_path: Path, age_seconds: int, usable: bool,
) -> None:
    settings = _settings(tmp_path)
    respx.get(f"{HA}/states/{GRID}").mock(return_value=_state(
        "100.0", last_reported=START - timedelta(seconds=age_seconds),
    ))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        result = await read_grid_import(settings, rest, START)
    assert (result.reading is not None) is usable
    if not usable:
        assert result.reason == "grid import reading is stale"


def _counter(path: Path, identity: str = "test-meter") -> SessionAllowanceCounter:
    return SessionAllowanceCounter(
        str(path), identity=identity, max_jump_kw=MAX_JUMP_KW,
    )


def _session(session_id: str = "hh-1", start: datetime = START,
             end: datetime = END) -> FreeSession:
    return FreeSession("happy_hour", session_id, start, end)


def _frozen_clock(frozen: datetime) -> type[datetime]:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Clock:
            at = frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)
            return cls.fromtimestamp(at.timestamp(), at.tzinfo)

    return _Clock


def _state(
    state: str, *, last_reported: datetime | None = None,
    unit: str = "kWh",
) -> httpx.Response:
    return httpx.Response(200, json={
        "entity_id": GRID,
        "state": state,
        "attributes": {"unit_of_measurement": unit},
        "last_reported": (last_reported or START).isoformat(),
    })


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = dict(
        ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "t.db"),
        soc_entity="sensor.soc", derive_grid_import_entity=GRID,
        free_electricity_enabled=False,
        octopus_api_url="http://octo.test/v1", octopus_api_key="sk_test",
        octopus_account_number="A-1234ABCD", octopus_mpan="mpan-test",
        octopus_backend_api_url="http://backend.test/v1",
    )
    base.update(kw)
    return Settings(**base)


async def _fake_happy(_s: Settings, *, now: datetime | None = None) -> HappyHourReading:
    return HappyHourReading(now or START, (
        HappyHourBooking("12345", START, END),
    ))


async def _fake_free(_s: Settings, *, now: datetime | None = None) -> object:
    raise AssertionError("Free Electricity is disabled for these tests")


async def test_start_reading_and_count_survive_a_restart(tmp_path: Path) -> None:
    session = _session()
    first = await _counter(tmp_path / "t.db").update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )
    assert first.remaining_kwh == {"hh-1": 16.0}

    counted = await _counter(tmp_path / "t.db").update(
        [session], reading=GridImport(104.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(minutes=30),
    )
    assert counted.remaining_kwh == {"hh-1": pytest.approx(12.0)}

    # A new process/counter object and a new session cache: the SQLite row is
    # the start baseline, not an in-memory assumption.
    restarted = await _counter(tmp_path / "t.db").update(
        [session], reading=GridImport(108.0, START + timedelta(minutes=45)),
        reason=None, now=START + timedelta(minutes=45),
    )
    assert restarted.remaining_kwh == {"hh-1": pytest.approx(8.0)}


async def test_mid_session_without_a_start_baseline_fails_closed(tmp_path: Path) -> None:
    session = _session()
    outcome = await _counter(tmp_path / "t.db").update(
        [session], reading=GridImport(104.0, START + timedelta(minutes=10)),
        reason=None, now=START + timedelta(minutes=10),
    )
    assert outcome.remaining_kwh == {}
    assert outcome.session_reasons == {"hh-1": "no trustworthy start baseline"}

    # A later reading must not become the baseline by stealth and invent 16 kWh.
    later = await _counter(tmp_path / "t.db").update(
        [session], reading=GridImport(106.0, START + timedelta(minutes=20)),
        reason=None, now=START + timedelta(minutes=20),
    )
    assert later.remaining_kwh == {}
    assert later.session_reasons == {"hh-1": "no trustworthy start baseline"}


async def test_backwards_and_implausible_readings_are_unusable(tmp_path: Path) -> None:
    session = _session()
    counter = _counter(tmp_path / "backwards.db")
    await counter.update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )

    backwards = await counter.update(
        [session], reading=GridImport(99.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(minutes=30),
    )
    assert backwards.remaining_kwh == {}
    assert backwards.session_reasons == {"hh-1": "grid import reading went backwards"}
    async with SessionAllowanceStore(str(tmp_path / "backwards.db")) as store:
        saved = await store.load("hh-1")
    assert saved is not None
    assert saved.trust_reason == "grid import reading went backwards"

    jump_counter = _counter(tmp_path / "jump.db")
    await jump_counter.update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )
    jump = await jump_counter.update(
        [session], reading=GridImport(200.0, START + timedelta(minutes=31)),
        reason=None, now=START + timedelta(minutes=31),
    )
    assert jump.remaining_kwh == {}
    assert jump.session_reasons == {"hh-1": "grid import jump is implausible"}


async def test_a_reset_latch_survives_a_later_catchup_and_restart(tmp_path: Path) -> None:
    session = _session()
    db_path = tmp_path / "t.db"
    await _counter(db_path).update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )
    await _counter(db_path).update(
        [session], reading=GridImport(104.0, START + timedelta(minutes=20)),
        reason=None, now=START + timedelta(minutes=20),
    )
    reset = await _counter(db_path).update(
        [session], reading=GridImport(99.0, START + timedelta(minutes=25)),
        reason=None, now=START + timedelta(minutes=25),
    )
    assert reset.remaining_kwh == {}
    assert reset.session_reasons == {"hh-1": "grid import reading went backwards"}

    # 105 is a plausible catch-up from 99, but the earlier reset already proved
    # this session's count unusable. A restart cannot clear that latch either.
    catchup = await _counter(db_path).update(
        [session], reading=GridImport(105.0, START + timedelta(minutes=45)),
        reason=None, now=START + timedelta(minutes=45),
    )
    assert catchup.remaining_kwh == {}
    assert catchup.session_reasons == {"hh-1": "grid import reading went backwards"}
    async with SessionAllowanceStore(str(db_path)) as store:
        saved = await store.load("hh-1")
    assert saved is not None
    assert saved.trust_reason == "grid import reading went backwards"


async def test_changed_source_identity_never_reuses_a_baseline(tmp_path: Path) -> None:
    session = _session()
    db_path = tmp_path / "t.db"
    await _counter(db_path, "meter-a").update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )

    switched = await _counter(db_path, "meter-b").update(
        [session], reading=GridImport(101.0, START + timedelta(minutes=10)),
        reason=None, now=START + timedelta(minutes=10),
    )
    assert switched.remaining_kwh == {}
    assert switched.session_reasons == {"hh-1": "grid import source identity changed"}

    restarted = await _counter(db_path, "meter-b").update(
        [session], reading=GridImport(102.0, START + timedelta(minutes=20)),
        reason=None, now=START + timedelta(minutes=20),
    )
    assert restarted.remaining_kwh == {}
    assert restarted.session_reasons == {"hh-1": "grid import source identity changed"}
    async with SessionAllowanceStore(str(db_path)) as store:
        saved = await store.load("hh-1")
    assert saved is not None
    assert saved.identity == "meter-a"
    assert saved.trust_reason == "grid import source identity changed"


async def test_counted_import_reaching_the_allowance_leaves_no_remaining(tmp_path: Path) -> None:
    session = _session()
    counter = _counter(tmp_path / "t.db")
    await counter.update(
        [session], reading=GridImport(100.0, START), reason=None, now=START,
    )
    exhausted = await counter.update(
        [session], reading=GridImport(116.0, START + timedelta(minutes=59)),
        reason=None, now=START + timedelta(minutes=59),
    )
    assert exhausted.remaining_kwh == {"hh-1": 0.0}


async def test_adjacent_hours_are_counted_separately(tmp_path: Path) -> None:
    first = _session("first")
    second = _session("second", START + timedelta(hours=1), START + timedelta(hours=2))
    counter = _counter(tmp_path / "t.db")
    await counter.update(
        [first, second], reading=GridImport(100.0, START), reason=None, now=START,
    )

    at_half = await counter.update(
        [first, second], reading=GridImport(105.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(minutes=30),
    )
    assert at_half.remaining_kwh == {
        "first": pytest.approx(11.0), "second": pytest.approx(16.0),
    }

    at_second_start = await counter.update(
        [first, second], reading=GridImport(105.0, START + timedelta(hours=1)),
        reason=None, now=START + timedelta(hours=1),
    )
    assert at_second_start.remaining_kwh == {
        "first": pytest.approx(11.0), "second": pytest.approx(16.0),
    }

    after_second_start = await counter.update(
        [first, second], reading=GridImport(108.0, START + timedelta(minutes=90)),
        reason=None, now=START + timedelta(minutes=90),
    )
    # The second hour starts counting at its own start baseline (105), never
    # pooling the first hour's 5 kWh.
    assert after_second_start.remaining_kwh == {
        "first": pytest.approx(8.0), "second": pytest.approx(13.0),
    }


async def test_second_hour_needs_its_own_start_boundary_reading(tmp_path: Path) -> None:
    first = _session("first")
    second = _session("second", START + timedelta(hours=1), START + timedelta(hours=2))
    db_path = tmp_path / "t.db"
    counter = _counter(db_path)
    await counter.update(
        [first, second], reading=GridImport(100.0, START), reason=None, now=START,
    )

    # At 12:30 the second hour has not started. Reporting its full allowance is
    # fine, but the 12:30 reading must not be persisted as its 13:00 baseline.
    future = await counter.update(
        [first, second], reading=GridImport(104.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(minutes=30),
    )
    assert future.remaining_kwh == {
        "first": pytest.approx(12.0), "second": pytest.approx(16.0),
    }
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("second") is None

    # No trustworthy reading at 13:00: the 13:30 reading cannot become the
    # second hour's start baseline by time passing.
    missing = await counter.update(
        [first, second], reading=GridImport(108.0, START + timedelta(minutes=90)),
        reason=None, now=START + timedelta(minutes=90),
    )
    assert missing.remaining_kwh == {"first": pytest.approx(8.0)}
    assert missing.session_reasons == {"second": "no trustworthy start baseline"}


async def test_a_pre_start_reading_cannot_become_the_next_hours_baseline(
    tmp_path: Path,
) -> None:
    second = _session("second", START + timedelta(hours=1), START + timedelta(hours=2))
    db_path = tmp_path / "t.db"

    # At 12:30 the hour has not started: report its full allowance, persist
    # nothing. A 12:30 reading is not the 13:00 start reading.
    before = await _counter(db_path).update(
        [second], reading=GridImport(104.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(minutes=30),
    )
    assert before.remaining_kwh == {"second": HAPPY_HOUR_ALLOWANCE_KWH}
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("second") is None

    # Time passing does not make that same pre-start reading authoritative.
    boundary = await _counter(db_path).update(
        [second], reading=GridImport(104.0, START + timedelta(minutes=30)),
        reason=None, now=START + timedelta(hours=1),
    )
    assert boundary.remaining_kwh == {}
    assert boundary.session_reasons == {"second": "no trustworthy start baseline"}

    # Nor can a later reading invent the missing boundary baseline.
    later = await _counter(db_path).update(
        [second], reading=GridImport(108.0, START + timedelta(minutes=90)),
        reason=None, now=START + timedelta(minutes=90),
    )
    assert later.remaining_kwh == {}
    assert later.session_reasons == {"second": "no trustworthy start baseline"}


async def test_old_session_rows_are_pruned_after_the_session_ends(tmp_path: Path) -> None:
    old = _session("old")
    counter = _counter(tmp_path / "t.db")
    await counter.update([old], reading=GridImport(100.0, START), reason=None, now=START)

    later = _session("later", START + timedelta(hours=3), START + timedelta(hours=4))
    await counter.update(
        [later], reading=GridImport(120.0, START + timedelta(hours=3)),
        reason=None, now=START + timedelta(hours=3),
    )
    async with SessionAllowanceStore(str(tmp_path / "t.db")) as store:
        assert await store.load("old") is None
        assert await store.load("later") is not None


async def test_cleanup_prunes_ended_rows_without_bookings(tmp_path: Path) -> None:
    old = _session("old")
    db_path = tmp_path / "t.db"
    await _counter(db_path).update(
        [old], reading=GridImport(100.0, START), reason=None, now=START,
    )

    await _counter(db_path).update(
        [], reading=None, reason=None, now=START + timedelta(hours=3),
    )
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("old") is None


async def test_cleanup_prunes_ended_rows_when_the_meter_is_unavailable(
    tmp_path: Path,
) -> None:
    old = _session("old")
    db_path = tmp_path / "t.db"
    await _counter(db_path).update(
        [old], reading=GridImport(100.0, START), reason=None, now=START,
    )

    outcome = await _counter(db_path).update(
        [old], reading=None, reason="grid import reading is stale",
        now=START + timedelta(hours=3),
    )
    assert outcome.reason == "grid import reading is stale"
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("old") is None


async def test_cleanup_cadence_survives_new_counter_instances(tmp_path: Path) -> None:
    old = _session("old")
    db_path = tmp_path / "t.db"
    await _counter(db_path).update(
        [old], reading=GridImport(100.0, START), reason=None, now=START,
    )

    first_cleanup = START + timedelta(hours=3)
    await _counter(db_path).update(
        [], reading=None, reason=None, now=first_cleanup,
    )
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("old") is None

    # Recreate an already-expired row after that cleanup slot. New counter
    # instances inside the same hour must not prune it again.
    await _counter(db_path).update(
        [old], reading=GridImport(100.0, START), reason=None, now=first_cleanup,
    )
    await _counter(db_path).update(
        [], reading=None, reason=None,
        now=first_cleanup + timedelta(minutes=5),
    )
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("old") is not None

    await _counter(db_path).update(
        [], reading=None, reason=None,
        now=first_cleanup + timedelta(hours=1, minutes=1),
    )
    async with SessionAllowanceStore(str(db_path)) as store:
        assert await store.load("old") is None


@pytest.mark.parametrize(
    "grid_response,expected_reason",
    [
        (None, "grid import entity is unset"),
        (httpx.Response(500, text="boom"), "grid import entity is unreadable"),
        (_state("100.0", last_reported=START - timedelta(hours=3)),
         "grid import reading is stale"),
    ],
    ids=["unset", "unreadable", "stale"],
)
@respx.mock
async def test_unusable_counter_removes_the_purchase_and_health_names_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    grid_response: httpx.Response | None, expected_reason: str,
) -> None:
    settings = _settings(
        tmp_path, derive_grid_import_entity=GRID if grid_response is not None else "",
    )
    monkeypatch.setattr(sources, "datetime", _frozen_clock(START))
    monkeypatch.setattr(health, "datetime", _frozen_clock(START))
    monkeypatch.setattr(sources, "fetch_happy_hours", _fake_happy)
    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", _fake_free)
    monkeypatch.setattr(health, "fetch_happy_hours", _fake_happy)

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{HA}/states/sensor.soc").mock(
        return_value=httpx.Response(200, json={
            "entity_id": "sensor.soc", "state": "20", "attributes": {},
            "last_reported": START.isoformat(),
        })
    )
    if grid_response is not None:
        respx.get(f"{HA}/states/{GRID}").mock(return_value=grid_response)
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(
            settings, rest, source=entities_source(settings),
            session_cache=SessionCache(clock=lambda: START),
            soc=SocMeasurement(SocStatus.OK, observed_at=START, value=20.0,
                               reported_at=START),
        )
    assert run.inputs.happy_hour_remaining_kwh == {}
    assert not any(p.overlay == "happy_hour" for p in run.plan.daytime_purchases)

    row = await health.check_happy_hour(settings)
    assert row.name == "Happy Hour"
    assert "booked:" in row.detail
    assert f"allowance unusable: {expected_reason}" in row.detail


@respx.mock
async def test_a_valid_reading_reaches_the_planner_and_survives_a_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(sources, "fetch_happy_hours", _fake_happy)
    monkeypatch.setattr(sources, "fetch_free_electricity_sessions", _fake_free)

    async def fake_load(*_a: object, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.get(f"{HA}/states/sensor.soc").mock(
        return_value=httpx.Response(200, json={
            "entity_id": "sensor.soc", "state": "20", "attributes": {},
            "last_reported": START.isoformat(),
        })
    )
    grid = respx.get(f"{HA}/states/{GRID}").mock(
        return_value=_state("100.0", last_reported=START)
    )
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))

    monkeypatch.setattr(sources, "datetime", _frozen_clock(START))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        first = await current_plan(
            settings, rest, source=entities_source(settings),
            session_cache=SessionCache(clock=lambda: START),
            soc=SocMeasurement(SocStatus.OK, observed_at=START, value=20.0,
                               reported_at=START),
        )
    assert first.inputs.happy_hour_remaining_kwh == {"12345": pytest.approx(16.0)}
    assert any(p.overlay == "happy_hour" for p in first.plan.daytime_purchases)

    second_at = START + timedelta(minutes=30)
    monkeypatch.setattr(sources, "datetime", _frozen_clock(second_at))
    grid.mock(return_value=_state("104.0", last_reported=second_at))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        second = await current_plan(
            settings, rest, source=entities_source(settings),
            session_cache=SessionCache(clock=lambda: second_at),
            soc=SocMeasurement(SocStatus.OK, observed_at=second_at, value=20.0,
                               reported_at=second_at),
        )
    assert second.inputs.happy_hour_remaining_kwh == {"12345": pytest.approx(12.0)}


@respx.mock
async def test_health_names_a_missing_start_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    later = START + timedelta(minutes=30)
    monkeypatch.setattr(health, "datetime", _frozen_clock(later))
    monkeypatch.setattr(health, "fetch_happy_hours", _fake_happy)
    respx.get(f"{HA}/states/{GRID}").mock(
        return_value=_state("104.0", last_reported=later)
    )

    row = await health.check_happy_hour(settings)
    assert "allowance unusable: no trustworthy start baseline" in row.detail
    assert not (tmp_path / "t.db").exists()


@respx.mock
async def test_health_does_not_create_a_missing_data_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "never-created.db"
    settings = _settings(tmp_path, db_path=str(db_path))
    monkeypatch.setattr(health, "datetime", _frozen_clock(START))
    monkeypatch.setattr(health, "fetch_happy_hours", _fake_happy)
    respx.get(f"{HA}/states/{GRID}").mock(return_value=_state("100.0", last_reported=START))

    row = await health.check_happy_hour(settings)
    assert "allowance unusable" not in row.detail
    assert not db_path.exists()


async def test_failed_schema_initialization_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    import aiosqlite

    import ha_spark.energy.session_allowance as allowance

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database")
    connect = aiosqlite.connect
    closed = []

    async def tracked_connect(db_path):
        conn = await connect(db_path)
        close = AsyncMock(wraps=conn.close)
        monkeypatch.setattr(conn, "close", close)
        closed.append(close)
        return conn

    monkeypatch.setattr(allowance.aiosqlite, "connect", tracked_connect)
    for _ in range(3):
        with pytest.raises(aiosqlite.DatabaseError):
            await _counter(path).update([], reading=None, reason=None, now=START)
    assert len(closed) == 3
    assert all(close.await_count == 1 for close in closed)


@respx.mock
async def test_database_probe_failure_preserves_ordinary_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)

    async def no_bookings(_s, *, now=None):
        return HappyHourReading(now or START, ())

    async def fake_load(*_a, **_kw):
        return LoadForecast(total_kwh=24.0, slots=(0.5,) * 48, source="test")

    exists = Path.exists

    def inaccessible(path):
        if str(path) == settings.db_path:
            raise PermissionError("inaccessible")
        return exists(path)

    monkeypatch.setattr(sources, "datetime", _frozen_clock(START))
    monkeypatch.setattr(sources, "fetch_happy_hours", no_bookings)
    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(Path, "exists", inaccessible)
    respx.route(method="GET", url__startswith=HA).mock(return_value=httpx.Response(404))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        run = await current_plan(
            settings, rest, source=entities_source(settings),
            session_cache=SessionCache(clock=lambda: START),
            soc=SocMeasurement(SocStatus.OK, observed_at=START, value=20.0,
                               reported_at=START),
        )
    assert run.plan is not None
    assert run.inputs.happy_hour_remaining_kwh == {}
    assert not any(p.overlay == "happy_hour" for p in run.plan.daytime_purchases)
