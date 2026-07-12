"""Tests for the daily scheduled plan/apply loop."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.api.server import AppState
from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.energy import scheduler, sources
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.energy.models import ChargeIntent, ChargePlan, LoadForecast
from ha_spark.energy.scheduler import (
    SIGNAL_SAMPLE_INTERVAL,
    guard_tick,
    run_forever,
    run_once,
    sample_signals,
    should_run,
)
from ha_spark.ha.rest import HomeAssistantRest

# A concrete intent so plan.charge_intent drives a real planned charge rate (W).
_INTENT = ChargeIntent(
    target_soc_pct=77.0, soc_now=30.0, window_start=time(23, 30), window_end=time(5, 30)
)


def _plan(intent: ChargeIntent = _INTENT) -> ChargePlan:
    return ChargePlan(
        soc_now=30, capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=2.69,
        deficit_kwh=12.8, buffer_pct=0.0, required_kwh=12.8,
        target_soc=77, window_hours=6.0,
        ev_charging=False, ha_template_needed=None,
        charge_intent=intent,
    )


def _planned_w(settings: Settings, intent: ChargeIntent) -> float:
    """The watts the active charger plans for ``intent`` (pure)."""
    rest = HomeAssistantRest(settings.ha_rest_url, settings.auth_token)
    return inverter_device(settings, rest).planned_rate_w(intent)


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

    assert plan.charge_intent.target_soc_pct >= 0
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

    def fake_make_server(_app: object, _host: str, _port: int) -> object:
        return object()  # never bound; serve_in_background is also stubbed

    async def fake_serve_in_background(_server: object) -> asyncio.Task[None]:
        return asyncio.ensure_future(asyncio.sleep(0))  # dummy completed task

    async def fake_stop_server(_server: object, task: asyncio.Task[None]) -> None:
        await task

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler, "make_server", fake_make_server)
    monkeypatch.setattr(scheduler, "serve_in_background", fake_serve_in_background)
    monkeypatch.setattr(scheduler, "stop_server", fake_stop_server)
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

    def fake_make_server(_app: object, _host: str, _port: int) -> object:
        return object()  # never bound; serve_in_background is also stubbed

    async def fake_serve_in_background(_server: object) -> asyncio.Task[None]:
        return asyncio.ensure_future(asyncio.sleep(0))  # dummy completed task

    async def fake_stop_server(_server: object, task: asyncio.Task[None]) -> None:
        await task

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler, "make_server", fake_make_server)
    monkeypatch.setattr(scheduler, "serve_in_background", fake_serve_in_background)
    monkeypatch.setattr(scheduler, "stop_server", fake_stop_server)
    return _StopLoop


async def test_run_forever_publishes_plan_to_api_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each computed plan is pushed into the AppState the HTTP API serves."""
    captured: dict[str, object] = {}

    def capture_build_app(state: object) -> object:
        captured["state"] = state
        return object()  # never actually served; make_server is stubbed too

    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])
    monkeypatch.setattr(scheduler, "build_app", capture_build_app)  # capture the AppState

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    state = captured["state"]
    assert isinstance(state, AppState)
    assert state.plan is not None


async def test_run_forever_guard_ticks_only_inside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard_targets: list[float | None] = []

    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def fake_guard_tick(_s: Settings, target_w: float | None) -> float:
        guard_targets.append(target_w)
        assert target_w is not None
        return target_w

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
    # Restore target is the plan's planned charge rate in watts, not amps.
    planned = _planned_w(s, _INTENT)
    assert guard_targets == [planned, planned]


async def test_run_forever_no_guard_when_entity_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def fail_guard_tick(_s: Settings, target_w: float | None) -> float:
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


async def test_run_forever_no_guard_when_charger_has_no_live_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AlphaESS has no settable rate -> the guard branch never fires, even with
    grid_power_entity set."""

    async def fake_run_once(_s: Settings) -> ChargePlan:
        return _plan()

    async def fail_guard_tick(_s: Settings, target_w: float | None) -> float:
        raise AssertionError("guard must not run for an inverter without a live rate")

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fail_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    stop = _patch_loop(monkeypatch, [datetime(2026, 6, 10, 23, 45)])

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        inverter="alphaess", grid_power_entity="sensor.house_supply_power",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)


async def test_run_forever_guard_failure_does_not_kill_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[datetime] = []

    async def boom_guard_tick(_s: Settings, target_w: float | None) -> float:
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
        grid_power_entity="sensor.house_supply_power", battery_voltage_v=51.0,
    )
    for entity, state in ((s.grid_power_entity, "2000"), (s.charge_current_entity, "30")):
        respx.get(f"http://ha.test/api/states/{entity}").mock(
            return_value=httpx.Response(
                200, json={"entity_id": entity, "state": state, "attributes": {}}
            )
        )
    # Mid-window restart: no plan target yet -> adopt the live setpoint in watts
    # (30 A * 51 V = 1530 W) via the charger's read_charge_rate.
    assert await guard_tick(s, None) == 30.0 * 51.0


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


@respx.mock
async def test_run_once_logs_proactive_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="simulate",
        db_path=str(tmp_path / "ledger.db"), timezone="UTC",
    )
    # Seed low occupancy so a suggestion fires, and no away context.
    async with ForecastLedger(s.db_path) as ledger:
        for d in range(14, 0, -1):
            day = datetime.now(UTC) - timedelta(days=d)
            await ledger.record_signal(
                day.replace(hour=12, minute=0, second=0, microsecond=0),
                "occupancy_home_frac", 0.05,
            )

    with caplog.at_level("INFO"):
        await run_once(s)

    assert any("Proactive decision" in r.message for r in caplog.records)
