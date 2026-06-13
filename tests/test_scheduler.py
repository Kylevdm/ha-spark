"""Tests for the daily scheduled plan/apply loop."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import scheduler, sources
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargePlan, LoadForecast
from ha_spark.energy.scheduler import (
    SIGNAL_SAMPLE_INTERVAL,
    guard_tick,
    run_forever,
    run_once,
    sample_signals,
    should_run,
)


def _plan(current_a: float = 42) -> ChargePlan:
    return ChargePlan(
        soc_now=30, capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=2.69,
        deficit_kwh=12.8, buffer_pct=0.0, required_kwh=12.8,
        target_soc=77, overnight_current_a=current_a, window_hours=6.0,
        ev_charging=False, ha_template_needed=None, actions=(),
    )


def test_should_run_at_or_after_run_time_once_per_day() -> None:
    run_time = time(22, 0)
    assert should_run(datetime(2026, 6, 10, 22, 0), run_time, None) is True
    assert should_run(datetime(2026, 6, 10, 23, 59), run_time, None) is True


def test_should_run_false_before_run_time() -> None:
    run_time = time(22, 0)
    assert should_run(datetime(2026, 6, 10, 21, 59), run_time, None) is False


def test_should_run_false_if_already_run_today() -> None:
    run_time = time(22, 0)
    assert should_run(datetime(2026, 6, 10, 22, 30), run_time, date(2026, 6, 10)) is False


def test_should_run_true_again_next_day() -> None:
    run_time = time(22, 0)
    # New day at midnight: not yet time again until 22:00.
    assert should_run(datetime(2026, 6, 11, 0, 0), run_time, date(2026, 6, 10)) is False
    assert should_run(datetime(2026, 6, 11, 22, 0), run_time, date(2026, 6, 10)) is True


@respx.mock
async def test_run_once_computes_and_applies_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="off",
        db_path=str(tmp_path / "ledger.db"),
    )

    with caplog.at_level("INFO"):
        plan = await run_once(s)

    assert plan.overnight_current_a >= 0
    assert any("Charge plan" in r.message for r in caplog.records)
    assert any("OFF" in r.message for r in caplog.records)

    tomorrow = (datetime.now(load_timezone(s.timezone)) + timedelta(days=1)).date()
    async with ForecastLedger(s.db_path) as ledger:
        rows = await ledger.forecasts_since(tomorrow)
    assert len(rows) == 1
    assert rows[0].target_date == tomorrow
    assert rows[0].model == "baseline"
    assert rows[0].total_kwh == plan.load_kwh
    assert rows[0].source == "test"


async def test_run_forever_runs_once_per_day_and_retries_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_run_once(_s: Settings) -> ChargePlan:
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("boom")
        return _plan()

    class _StopLoop(Exception):
        pass

    # Tick sequence: fail at 22:00, retry succeeds at 22:00 (still same day),
    # then no more runs until the next day at 22:00.
    ticks = iter(
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 23, 0),
            datetime(2026, 6, 11, 22, 0),
        ]
    )

    class _FakeDatetime:
        @staticmethod
        def now(_tz: object) -> datetime:
            try:
                return next(ticks)
            except StopIteration as exc:
                raise _StopLoop from exc

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(_StopLoop):
        await run_forever(s, poll_seconds=0)

    # First tick fails (retry), second tick (still 22:00) succeeds, third
    # tick (23:00, already run today) skips, fourth tick (next day) runs again.
    assert calls == ["run", "run", "run"]


def _patch_loop(
    monkeypatch: pytest.MonkeyPatch, ticks: list[datetime]
) -> type[Exception]:
    """Drive run_forever through ``ticks``, then raise to stop the loop."""
    it = iter(ticks)

    class _StopLoop(Exception):
        pass

    class _FakeDatetime:
        @staticmethod
        def now(_tz: object) -> datetime:
            try:
                return next(it)
            except StopIteration as exc:
                raise _StopLoop from exc

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    return _StopLoop


async def test_run_forever_guard_ticks_only_inside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_targets: list[float | None] = []

    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan(42)

    async def fake_guard_tick(_s: Settings, target_a: float | None) -> float:
        guard_targets.append(target_a)
        assert target_a is not None
        return target_a

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fake_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),  # plan runs; outside window -> no guard
            datetime(2026, 6, 10, 23, 45),  # inside window -> guard ticks
            datetime(2026, 6, 11, 4, 0),  # still inside (wraps midnight) -> guard ticks
            datetime(2026, 6, 11, 12, 0),  # outside window -> no guard
        ],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        grid_power_entity="sensor.house_supply_power",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    assert guard_targets == [42, 42]


async def test_run_forever_no_guard_when_entity_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def fail_guard_tick(_s: Settings, target_a: float | None) -> float:
        raise AssertionError("guard must not run when grid_power_entity is empty")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fail_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 23, 45)])

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)


async def test_run_forever_guard_failure_does_not_kill_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[datetime] = []

    async def boom_guard_tick(_s: Settings, target_a: float | None) -> float:
        attempts.append(datetime.now())
        raise RuntimeError("HA unreachable")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "guard_tick", boom_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [datetime(2026, 6, 11, 0, 0), datetime(2026, 6, 11, 0, 1)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        grid_power_entity="sensor.house_supply_power",
    )
    with caplog.at_level("ERROR"), pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    assert len(attempts) == 2  # the failure did not stop the next tick
    assert any("Supply guard tick failed" in r.message for r in caplog.records)


@respx.mock
async def test_guard_tick_adopts_setpoint_as_target_on_restart() -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="simulate",
        grid_power_entity="sensor.house_supply_power",
    )
    for entity, state in ((s.grid_power_entity, "2000"), (s.charge_current_entity, "30")):
        respx.get(f"http://ha.test/api/states/{entity}").mock(
            return_value=httpx.Response(
                200, json={"entity_id": entity, "state": state, "attributes": {}}
            )
        )
    # Mid-window restart: no plan target yet -> adopt the live 30 A setpoint.
    assert await guard_tick(s, None) == 30.0


@respx.mock
async def test_sample_signals_records_occupancy_heatpump_and_temperature(
    tmp_path: Path,
) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="person.alice, person.bob",
        heatpump_energy_entity="sensor.heatpump_energy",
        outdoor_weather_entity="weather.home",
    )
    respx.get("http://ha.test/api/states/person.alice").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "person.alice", "state": "home", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/person.bob").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "person.bob", "state": "not_home", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/sensor.heatpump_energy").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.heatpump_energy", "state": "1.5", "attributes": {}}
        )
    )
    respx.get("http://ha.test/api/states/weather.home").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "weather.home",
                "state": "cloudy",
                "attributes": {"temperature": 12.5},
            },
        )
    )

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == [(now, 0.5)]
        assert await ledger.signal_history("heatpump_kwh", since) == [(now, 1.5)]
        assert await ledger.signal_history("temp_out_c", since) == [(now, 12.5)]


@respx.mock
async def test_sample_signals_skips_disabled_signals(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="",
        heatpump_energy_entity="",
        outdoor_weather_entity="",
    )
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == []
        assert await ledger.signal_history("heatpump_kwh", since) == []
        assert await ledger.signal_history("temp_out_c", since) == []


@respx.mock
async def test_sample_signals_tolerates_unreadable_entity(tmp_path: Path) -> None:
    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        db_path=str(tmp_path / "ledger.db"),
        person_entities="person.alice",
        heatpump_energy_entity="",
        outdoor_weather_entity="",
    )
    respx.get("http://ha.test/api/states/person.alice").mock(return_value=httpx.Response(500))

    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)
    await sample_signals(s, now)  # must not raise

    async with ForecastLedger(s.db_path) as ledger:
        since = datetime(2026, 1, 1, tzinfo=UTC)
        assert await ledger.signal_history("occupancy_home_frac", since) == [(now, 0.0)]


async def test_run_forever_samples_signals_every_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sampled: list[datetime] = []

    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def fake_sample_signals(_s: Settings, now: datetime) -> None:
        sampled.append(now)

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", fake_sample_signals)
    stop = _patch_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL - timedelta(minutes=1),
            datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL,
        ],
    )

    s = Settings(ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "ledger.db"))
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # First tick samples immediately; the next sample is skipped until the
    # interval elapses, then samples again on the third tick.
    assert sampled == [
        datetime(2026, 6, 10, 22, 0),
        datetime(2026, 6, 10, 22, 0) + SIGNAL_SAMPLE_INTERVAL,
    ]
