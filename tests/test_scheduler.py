"""Tests for the daily scheduled plan/apply loop."""

from __future__ import annotations

from datetime import date, datetime, time

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.scheduler import run_forever, run_once, should_run


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
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fake_load(_s: Settings) -> LoadForecast:
        return LoadForecast(total_kwh=24.0, slots=None, source="test")

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    s = Settings(ha_url="http://ha.test", ha_token="t", proactive_mode="off")

    with caplog.at_level("INFO"):
        await run_once(s)

    assert any("Charge plan" in r.message for r in caplog.records)
    assert any("OFF" in r.message for r in caplog.records)


async def test_run_forever_runs_once_per_day_and_retries_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_run_once(_s: Settings) -> None:
        calls.append("run")
        if len(calls) == 1:
            raise RuntimeError("boom")

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

    monkeypatch.setattr(scheduler, "datetime", _FakeDatetime)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    s = Settings(ha_url="http://ha.test", ha_token="t", plan_run_time="22:00")
    with pytest.raises(_StopLoop):
        await run_forever(s, poll_seconds=0)

    # First tick fails (retry), second tick (still 22:00) succeeds, third
    # tick (23:00, already run today) skips, fourth tick (next day) runs again.
    assert calls == ["run", "run", "run"]
