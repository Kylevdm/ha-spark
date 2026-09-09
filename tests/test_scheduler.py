"""Tests for the daily scheduled plan/apply loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


# A concrete intent so plan.charge_intent drives a real planned charge rate (W).
_INTENT = ChargeIntent(
    target_soc_pct=77.0, soc=_soc(30.0), window_start=time(23, 30), window_end=time(5, 30)
)


def _plan(intent: ChargeIntent = _INTENT) -> ChargePlan:
    return ChargePlan(
        soc=_soc(30), capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
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


@respx.mock
async def test_dynamic_plan_parity_between_scheduler_and_agent_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#91: under ``tariff_provider="dynamic"`` the daily scheduler and the agent
    ``get_plan`` surface must cost against the *same* live prices. Before the fix
    ``get_plan`` silently used the fixed fallback, so its cost differed from the
    plan the daemon applied.

    The issue frames this as identical ``slot_prices``, but the agent payload
    never exposes ``slot_prices`` (see ``plan_to_payload``); ``baseline_cost``
    sums each slot at its live import price, so it is the faithful price-derived
    observable that stands in for the raw prices here."""
    from ha_spark.agent import tools

    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        # 48 slot-of-day kWh -> triggers the v2 slot horizon (and slot_prices).
        return LoadForecast(total_kwh=24.0, slots=tuple(0.5 for _ in range(48)), source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)

    # One wide live-rate point spanning the whole horizon at a distinctive price,
    # so every slot is priced 0.99 regardless of today's date — nothing like the
    # fixed 0.069/0.30, so a fixed-fallback bug would show up in the cost.
    now = datetime.now(UTC)
    rates = [{
        "start": (now - timedelta(days=1)).isoformat(),
        "end": (now + timedelta(days=3)).isoformat(),
        "value_inc_vat": 0.99,
    }]
    respx.get("http://ha.test/api/states/sensor.dynamic_rates").mock(
        return_value=httpx.Response(
            200, json={"entity_id": "sensor.dynamic_rates", "state": "0.99",
                       "attributes": {"rates": rates}},
        )
    )
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="off",
        db_path=str(tmp_path / "ledger.db"),
        tariff_provider="dynamic", dynamic_rates_entity="sensor.dynamic_rates",
    )

    plan = await run_once(s)
    # The dynamic schedule actually flowed through the daemon's plan.
    assert plan.model == "slots"
    assert plan.slot_prices == tuple(0.99 for _ in range(48))

    # baseline_cost tallies each slot at its live import price, so it reflects the
    # 0.99 dynamic prices (unlike planned_cost, which costs at the representative
    # cheap/standard rates). Under the old bug get_plan would report the fixed
    # fallback's baseline here, diverging from the daemon's.
    result = await tools.get_plan(s)
    baseline_cost = next(
        e["state"] for e in result.plan if e["entity_id"] == "sensor.ha_spark_baseline_cost"
    )
    assert baseline_cost == f"{plan.baseline_cost:.2f}"


async def test_run_forever_runs_once_per_day_and_retries_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
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

    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
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

    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
        return _plan()

    async def fake_guard_tick(
        _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
    ) -> float:
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
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)
    # Restore target is the plan's planned charge rate in watts, not amps.
    planned = _planned_w(s, _INTENT)
    assert guard_targets == [planned, planned]


async def test_run_forever_no_guard_when_entity_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
        return _plan()

    async def fail_guard_tick(
        _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
    ) -> float:
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

    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
        return _plan()

    async def fail_guard_tick(
        _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
    ) -> float:
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

    async def boom_guard_tick(
        _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
    ) -> float:
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
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
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
    for entity, state in (
        (s.grid_power_entity, "2000"),
        ("sensor.solis_control_timed_charge_current", "30"),
    ):
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

    async def fake_run_once(_s: Settings, *, soc: SocMeasurement | None = None) -> ChargePlan:
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


@respx.mock
async def test_run_once_triggers_derived_rerive_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheduled plan run also re-derives trailing base-load history when configured."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    rerive_calls: list[object] = []

    async def fake_rerive(
        settings: Settings,
        specs: dict[str, object],
        *,
        statistic_id: str,
        statistic_name: str,
        window_hours: int = 48,
    ) -> object:
        rerive_calls.append(specs)
        from datetime import UTC, datetime

        from ha_spark.energy.derived_base_load import DerivedBackfillResult
        return DerivedBackfillResult(
            rows_imported=10,
            span="2026-06-11 00:00 .. 2026-06-11 09:00 UTC",
            degradation=[],
            negative_clamped=0,
            coverage={"grid_import": (datetime(2026, 6, 10, tzinfo=UTC),
                                      datetime(2026, 6, 11, tzinfo=UTC))},
        )

    monkeypatch.setattr(scheduler, "rerive_trailing_window", fake_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="off",
        db_path=str(tmp_path / "ledger.db"),
        derive_grid_import_entity="sensor.grid_import",
    )
    with caplog.at_level("INFO"):
        await run_once(s)
    assert len(rerive_calls) == 1
    assert "10 rows upserted" in caplog.text


@respx.mock
async def test_run_once_skips_derived_rerive_without_grid_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No grid import configured -> rerive is a no-op (source-entity path still works)."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    rerive_calls: list[object] = []

    async def fake_rerive(*args: object, **kwargs: object) -> object:
        rerive_calls.append(args)
        raise AssertionError("should not be called without grid import")

    monkeypatch.setattr(scheduler, "rerive_trailing_window", fake_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="off",
        db_path=str(tmp_path / "ledger.db"),
    )
    await run_once(s)
    assert rerive_calls == []


@respx.mock
async def test_run_once_rerive_failure_does_not_block_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rerive failure is logged but never aborts the plan run."""
    async def fake_load(_s: Settings, **_kw: object) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    async def boom_rerive(*args: object, **kwargs: object) -> object:
        raise RuntimeError("ws down")

    monkeypatch.setattr(scheduler, "rerive_trailing_window", boom_rerive)

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="off",
        db_path=str(tmp_path / "ledger.db"),
        derive_grid_import_entity="sensor.grid_import",
    )
    with caplog.at_level("INFO"):
        await run_once(s)
    assert any("Derived base-load rerive failed" in r.message for r in caplog.records)
    # The plan still ran successfully.
    assert any("Charge plan" in r.message for r in caplog.records)


def test_derive_specs_for_scheduler_uses_shared_helper() -> None:
    """The scheduler reads the shared derive_specs_from_settings helper.

    A duplicate helper here would let the CLI and scheduler drift apart
    (different defaults for the same Settings options); the test pins
    that both code paths read from the same module-level mapper.
    """
    from ha_spark.energy import scheduler
    from ha_spark.energy.derived_base_load import derive_specs_from_settings

    s = Settings(
        ha_url="http://ha.test", ha_token="t",
        derive_grid_import_entity="sensor.gi",
        derive_solar_generation_entity="sensor.sol",
        derive_invert_grid_export=True,
    )
    # The scheduler module exposes the same helper, not its own copy.
    assert scheduler.derive_specs_from_settings is derive_specs_from_settings
    specs = derive_specs_from_settings(s)
    assert specs["grid_import"].entity_id == "sensor.gi"
    assert specs["solar_generation"].entity_id == "sensor.sol"
    # Grid export is not configured (no entity) but the invert flag is set;
    # the helper must not include it when no entity id is present.
    assert "grid_export" not in specs


# --- SoC integrity monitoring in the one-minute loop (#114) ---


def _soc_resp(state: str, reported: datetime) -> httpx.Response:
    """One HA SoC-entity response; ``reported`` drives the freshness check."""
    return httpx.Response(
        200,
        json={
            "entity_id": "sensor.soc",
            "state": state,
            "attributes": {},
            "last_reported": reported.isoformat(),
        },
    )


def _soc_get(states: list[str], reported: list[datetime]) -> None:
    """Mock the SoC entity read with one response per daemon tick."""
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        side_effect=[
            _soc_resp(state, rep) for state, rep in zip(states, reported, strict=True)
        ]
    )


def _integrity_posts() -> list[tuple[str, dict[str, object]]]:
    """The (state, attributes) of every soc-integrity sensor push this test saw."""
    out: list[tuple[str, dict[str, object]]] = []
    for call in respx.calls:
        if call.request.url.path != "/api/states/sensor.ha_spark_soc_integrity":
            continue
        body = json.loads(call.request.content)
        out.append((body["state"], body["attributes"]))
    return out


def _patch_monitor_loop(
    monkeypatch: pytest.MonkeyPatch,
    ticks: list[datetime],
    *,
    run_once_socs: list[SocMeasurement | None] | None = None,
    guard_socs: list[SocMeasurement | None] | None = None,
) -> type[Exception]:
    """Patch the loop like ``_patch_loop``, optionally capturing tick SoCs."""
    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    if run_once_socs is not None:
        async def fake_run_once(
            _s: Settings, *, soc: SocMeasurement | None = None
        ) -> ChargePlan:
            run_once_socs.append(soc)
            return _plan()
    else:
        async def fake_run_once(
            _s: Settings, *, soc: SocMeasurement | None = None
        ) -> ChargePlan:
            return _plan()

    if guard_socs is not None:
        async def fake_guard_tick(
            _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
        ) -> float:
            guard_socs.append(soc)
            assert target_w is not None
            return target_w
    else:
        async def fake_guard_tick(
            _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
        ) -> float:
            assert target_w is not None
            return target_w

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", fake_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    return _patch_loop(monkeypatch, ticks)


@respx.mock
async def test_loop_isolated_soc_failure_then_pass_resets_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad minute is tolerated: pending failure, then a passing observation
    resets the count and returns to normal operation."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    now = datetime.now(UTC)
    _soc_get(["unavailable", "55"], [now, now])
    run_socs: list[SocMeasurement | None] = []
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1)],
        run_once_socs=run_socs,
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # The plan run consumed the tick's failed measurement (blocked at the
    # charger gate), and the passing tick reset the count.
    assert run_socs[0] is not None and not run_socs[0].ok
    assert run_socs[0].status is SocStatus.UNAVAILABLE
    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure", "normal"]
    assert published[0][1]["consecutive_failures"] == 1
    assert published[1][1]["consecutive_failures"] == 0
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 0
    }


@respx.mock
async def test_loop_third_consecutive_failure_reaches_fallback_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The default third consecutive failed observation reaches the fallback-
    entry threshold (the fallback write itself lands with #115)."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50", "50"], [stale, stale, stale])
    stop = _patch_monitor_loop(
        monkeypatch,
        [
            datetime(2026, 6, 10, 22, 0),
            datetime(2026, 6, 10, 22, 1),
            datetime(2026, 6, 10, 22, 2),
        ],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        v2l_power_entity="",
    )
    with caplog.at_level("WARNING"), pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    published = _integrity_posts()
    assert [state for state, _ in published] == [
        "pending_failure",
        "pending_failure",
        "fallback_threshold",
    ]
    assert published[-1][1]["consecutive_failures"] == 3
    assert published[-1][1]["failure_threshold"] == 3
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 3
    }
    assert "fallback-entry threshold reached" in caplog.text


@respx.mock
async def test_loop_custom_threshold_behaves_equivalently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50", "50"], [stale, stale])
    stop = _patch_monitor_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 22, 0), datetime(2026, 6, 10, 22, 1)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        soc_failure_threshold=2, v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure", "fallback_threshold"]
    assert published[-1][1]["failure_threshold"] == 2


@respx.mock
async def test_loop_observes_once_per_tick_and_reuses_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observation identity: one failed tick where the plan run, the guard, and
    publication all consume the same observation increments the count once."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    soc_get = respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=_soc_resp("50", stale)
    )
    run_socs: list[SocMeasurement | None] = []
    guard_socs: list[SocMeasurement | None] = []
    # 23:30: plan run time AND inside the charge window -> both fire this tick.
    stop = _patch_monitor_loop(
        monkeypatch, [datetime(2026, 6, 10, 23, 30)],
        run_once_socs=run_socs, guard_socs=guard_socs,
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="23:30",
        charge_window_start="23:30", charge_window_end="05:30",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    # Exactly one HA read for the SoC this tick, and the very same measurement
    # object reached the plan run and the guard.
    assert soc_get.call_count == 1
    assert run_socs[0] is guard_socs[0]
    assert run_socs[0] is not None and not run_socs[0].ok
    # Counted once, not once per consumer.
    assert json.loads((tmp_path / "ha_spark_soc_monitor.json").read_text()) == {
        "consecutive_failures": 1
    }
    published = _integrity_posts()
    assert [state for state, _ in published] == ["pending_failure"]
    assert published[0][1]["soc_status"] == "stale"


@respx.mock
async def test_loop_skips_monitoring_without_soc_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No configured soc_entity -> nothing to observe: no reads, no publishes,
    no persisted monitor state (the plan run observes through its own path)."""
    gets = respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stop = _patch_monitor_loop(monkeypatch, [datetime(2026, 6, 10, 22, 0)])

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="22:00",
        db_path=str(tmp_path / "ledger.db"), v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert gets.call_count == 0
    assert _integrity_posts() == []
    assert not (tmp_path / "ha_spark_soc_monitor.json").exists()


def _failed_soc() -> SocMeasurement:
    """One stale checked measurement: parseable value, unusably old report."""
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.STALE,
        observed_at=now,
        value=30.0,
        raw_state="30",
        reported_at=now - timedelta(hours=1),
        age_s=3600.0,
        max_age_s=600.0,
    )


async def _fake_load(_s: Settings, **_kw: object) -> LoadForecast:
    return LoadForecast(total_kwh=24.0, slots=None, source="test")


@respx.mock
async def test_run_once_failed_soc_leaves_solis_resident_program_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First-failure policy on Solis: the plan run consumes the tick's failed
    measurement, no register is written (resident program untouched), and the
    blocked action line names the concrete integrity reason."""
    monkeypatch.setattr(sources, "predict_home_load", _fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="on",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
    )
    failed = _failed_soc()
    with caplog.at_level("WARNING"):
        plan = await run_once(s, soc=failed)

    # The plan carries the exact tick measurement (no independent reread).
    assert plan.soc is failed
    assert any("[BLOCKED]" in r.message and "not charging to" in r.message
               for r in caplog.records)
    assert any("over the 600s maximum" in r.message for r in caplog.records)
    # No service call left the process: the resident program is untouched
    # (only reads and sensor publishes happened).
    for call in respx.calls:
        assert not call.request.url.path.startswith("/api/services/")


@respx.mock
async def test_run_once_failed_soc_blocks_alphaess_programming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """First-failure policy on AlphaESS: any failed integrity observation
    blocks new charge programming (no fallback, no service call)."""
    monkeypatch.setattr(sources, "predict_home_load", _fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))

    s = Settings(
        ha_url="http://ha.test", ha_token="t", proactive_mode="on",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        inverter="alphaess", alphaess_serial="SN1",
    )
    with caplog.at_level("INFO"):
        plan = await run_once(s, soc=_failed_soc())

    assert not plan.soc.ok
    assert any("[BLOCKED]" in r.message and "not charge to" in r.message
               for r in caplog.records)
    for call in respx.calls:
        assert not call.request.url.path.startswith("/api/services/")


@respx.mock
async def test_loop_blocked_plan_rate_never_becomes_guard_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan blocked on an untrusted SoC was sized from soc_now == 0 (likely
    max current): its rate must not become the supply guard's restore target.
    The guard adopts the live setpoint instead — reductions only."""
    respx.route(method="POST").mock(return_value=httpx.Response(200, json={}))
    stale = datetime.now(UTC) - timedelta(hours=1)
    _soc_get(["50"], [stale])

    async def fake_run_once(
        _s: Settings, *, soc: SocMeasurement | None = None
    ) -> ChargePlan:
        # A plan computed from the tick's failed measurement: blocked at the
        # charger gate, but still a plan object (existence != applied).
        failed = soc if soc is not None and not soc.ok else _failed_soc()
        return replace(
            _plan(ChargeIntent(
                target_soc_pct=90.0, soc=failed,
                window_start=time(23, 30), window_end=time(5, 30),
            )),
            soc=failed,  # compute_plan carries the same measurement at both levels
        )

    guard_targets: list[float | None] = []

    async def capture_guard_tick(
        _s: Settings, target_w: float | None, *, soc: SocMeasurement | None = None
    ) -> float:
        guard_targets.append(target_w)
        # Like the real guard_tick: adopt (echo) the live setpoint when no
        # trusted target exists, so later ticks keep it as the ceiling.
        return target_w if target_w is not None else 2040.0

    async def noop_sample_signals(_s: Settings, _now: datetime) -> None:
        return None

    planned_calls: list[object] = []

    async def fake_planned_rate_w(_s: Settings, _plan: ChargePlan) -> float:
        planned_calls.append(_plan)
        return 4000.0

    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "guard_tick", capture_guard_tick)
    monkeypatch.setattr(scheduler, "sample_signals", noop_sample_signals)
    monkeypatch.setattr(scheduler, "_planned_rate_w", fake_planned_rate_w)
    stop = _patch_loop(
        monkeypatch,
        [datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 45)],
    )

    s = Settings(
        ha_url="http://ha.test", ha_token="t", plan_run_time="23:30",
        charge_window_start="23:30", charge_window_end="05:30",
        db_path=str(tmp_path / "ledger.db"), soc_entity="sensor.soc",
        grid_power_entity="sensor.house_supply_power", v2l_power_entity="",
    )
    with pytest.raises(stop):
        await run_forever(s, poll_seconds=0)

    assert planned_calls == []  # the blocked plan's rate was never consulted
    # First in-window tick: no trusted target (None) -> the guard adopts the
    # live setpoint; the adopted value, not the blocked plan's rate, is the
    # ceiling the second tick sees.
    assert guard_targets == [None, 2040.0]
