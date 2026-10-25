"""Caller-to-loop tests: live fallback context, not independent one-shot drivers."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from ha_spark import cli
from ha_spark.agent import tools
from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, submit_socket
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.energy import scheduler
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from tests.authority_helpers import seed_authority, seed_settings


def settings_at(path: Path, **kwargs: object) -> Settings:
    settings = Settings(  # type: ignore[arg-type]
        ha_url="http://ha.test", ha_token="SECRET", db_path=str(path / "t.db"),
        agent_surface="on", agent_exposure="read_act", **kwargs,
    )
    seed_settings(path, settings=settings)
    seed_authority(settings, "optimise")
    return settings


async def invoke(caller: str, settings: Settings, state: AppState) -> object:
    if caller == "apply":
        return await cli._cmd_plan(settings, apply=True)
    if caller == "once":
        return await cli._cmd_run(settings, once=True)
    if caller == "clock":
        return await cli._cmd_solis_sync_clock(settings)
    if caller == "tool":
        return await tools.run_plan(settings, executor=state.executor)
    if caller == "mcp":
        _, payload = await build_mcp(state).call_tool("run_plan", {})
        return payload
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(state)), base_url="http://local"
    ) as client:
        response = await client.post("/agent/run")
    assert response.status_code == 200
    return response.json()


CALLERS = ["apply", "once", "clock", "tool", "mcp", "route"]


@pytest.mark.parametrize("caller", CALLERS)
@respx.mock
async def test_without_daemon_computes_but_never_actuates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caller: str
) -> None:
    settings = settings_at(tmp_path)
    respx.get(url__regex=r"http://ha.test/api/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    services = respx.post(url__regex=r"http://ha.test/api/services/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    device = AsyncMock()
    factory = AsyncMock(return_value=device)
    monkeypatch.setattr(scheduler, "inverter_device", factory)
    state = AppState(settings, tmp_path / "options.json")
    result = await invoke(caller, settings, state)
    output = capsys.readouterr().out
    assert not services.called
    factory.assert_not_called()
    assert "SECRET" not in output + str(result)
    if caller in ("apply", "once", "clock"):
        assert result == (1 if caller == "clock" else 0)
        assert "[REFUSED] no ha-spark daemon" in output
        if caller != "clock":
            assert "plan" in output.lower()
    else:
        data = result.model_dump() if isinstance(result, tools.PlanResult) else result
        assert data["plan"]
        assert "[REFUSED] no ha-spark daemon" in data["refusal"]


@asynccontextmanager
async def running_daemon(settings: Settings, path: Path, monkeypatch: pytest.MonkeyPatch):
    seed_settings(path, settings=settings)
    captured = {}
    ready = asyncio.Event()
    planned = asyncio.Event()
    calls = []
    device = AsyncMock(spec=SolisDevice)
    device.reconcile_holds.return_value = []
    device.apply.return_value = [ActionLine("[APPLIED] unexpected", ActionOutcome.VERIFIED)]
    device.sync_clock.return_value = (
        "synced", [ActionLine("[APPLIED] daemon clock", ActionOutcome.VERIFIED)]
    )
    device.write_safe_state.return_value = []
    device.planned_rate_w = lambda intent: None
    monitor = SimpleNamespace(
        fallback_active=True, fallback_confirmed=True, recovery_ready=False,
        recovery_blocked=True, awaiting_fresh_observation=False,
    )
    measurement = SocMeasurement(
        status=SocStatus.OK, observed_at=datetime.now(UTC), reported_at=datetime.now(UTC),
        value=50, raw_state="50", age_s=0, max_age_s=600,
    )
    original_run = scheduler.run_once

    async def record_run(s, **kwargs):
        calls.append(kwargs)
        result = await original_run(s, **kwargs)
        planned.set()
        return result

    def capture_app(state, **kwargs):
        captured["state"] = state
        return object()

    async def serve(server):
        ready.set()
        return asyncio.create_task(asyncio.sleep(0))

    async def stop(server, task):
        await task

    monkeypatch.setattr(scheduler, "OPTIONS_PATH", path / "options.json")
    monkeypatch.setattr(scheduler, "build_app", capture_app)
    monkeypatch.setattr(scheduler, "make_server", lambda *args: object())
    monkeypatch.setattr(scheduler, "serve_in_background", serve)
    monkeypatch.setattr(scheduler, "stop_server", stop)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *args: device)
    monkeypatch.setattr(scheduler.SocMonitor, "load", lambda s: monitor)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", AsyncMock(return_value=measurement))
    for name in ("sample_signals", "_record_forecast", "_run_derived_rerive"):
        monkeypatch.setattr(scheduler, name, AsyncMock())
    monkeypatch.setattr(scheduler, "run_once", record_run)
    task = asyncio.create_task(scheduler.run_forever(settings, poll_seconds=0))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        await asyncio.wait_for(planned.wait(), 2)
        yield captured["state"], calls, device, measurement
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("caller", CALLERS)
@respx.mock
async def test_with_daemon_uses_live_fallback_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], caller: str
) -> None:
    settings = settings_at(tmp_path)
    respx.get(url__regex=r"http://ha.test/api/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(url__regex=r"http://ha.test/api/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with running_daemon(settings, tmp_path, monkeypatch) as (state, calls, device, soc):
        result = await asyncio.wait_for(invoke(caller, settings, state), 3)
        device.apply.assert_not_called()
        if caller == "clock":
            assert result == 0
            device.sync_clock.assert_awaited_once()
        else:
            assert len(calls) >= 2  # startup tick plus this caller's queued run
            assert all(call["soc"] is soc and call["fallback_active"] for call in calls)
            assert all(call["ev_hold_state"] is calls[0]["ev_hold_state"] for call in calls)
            output = capsys.readouterr().out
            if caller in ("apply", "once"):
                assert result == 0
                assert "[SKIP] normal programming held" in output
            else:
                data = result.model_dump() if isinstance(result, tools.PlanResult) else result
                assert data["plan"]
                assert any("normal programming held" in line["text"] for line in data["actions"])
                assert any(line["outcome"] == "blocked" for line in data["actions"])


@respx.mock
async def test_request_failure_does_not_break_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_at(tmp_path)
    respx.get(url__regex=r"http://ha.test/api/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(url__regex=r"http://ha.test/api/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with running_daemon(settings, tmp_path, monkeypatch) as (state, calls, device, soc):
        original = scheduler.run_once
        monkeypatch.setattr(scheduler, "run_once", AsyncMock(side_effect=RuntimeError("SECRET")))
        reply = await asyncio.wait_for(submit_socket(settings, "apply"), 3)
        assert reply.error == "daemon plan request failed"
        assert "SECRET" not in reply.model_dump_json()
        monkeypatch.setattr(scheduler, "run_once", original)
        result = await asyncio.wait_for(tools.run_plan(settings, executor=state.executor), 3)
        assert result.plan and result.refusal is None
        assert calls[-1]["previous_plan"] is None
        device.sync_clock.side_effect = RuntimeError("SECRET")
        reply = await asyncio.wait_for(submit_socket(settings, "sync_clock"), 3)
        assert reply.error == "daemon clock request failed"
        assert "SECRET" not in reply.model_dump_json()
        assert "SECRET" not in caplog.text
        result = await asyncio.wait_for(tools.run_plan(settings, executor=state.executor), 3)
        assert result.plan and result.refusal is None


@respx.mock
async def test_second_daemon_cli_refused_before_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = settings_at(tmp_path)
    factory = AsyncMock()
    monkeypatch.setattr(scheduler, "inverter_device", factory)
    monkeypatch.setattr(scheduler, "OPTIONS_PATH", tmp_path / "options.json")
    services = respx.post(url__regex=r"http://ha.test/api/services/.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with DaemonChannel(settings):
        assert await cli._cmd_run(settings, once=False) == 1
    assert "already running" in capsys.readouterr().err
    factory.assert_not_called()
    assert not services.called
