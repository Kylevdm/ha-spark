"""Authority is the only grant: exercise real drivers over mocked HA REST."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import (
    AuthorityStore,
    FallbackState,
    HoldDevice,
    HoldRecord,
    Transition,
    load_authority,
)
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel
from ha_spark.devices import inverter_device
from ha_spark.devices.execution import ExecutionContext, Ownership
from ha_spark.devices.inverters.solis import SolisDevice
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.energy import scheduler, sources
from ha_spark.energy.models import LoadForecast
from ha_spark.energy.publish import publish_operating_mode
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import Status, check_operating_mode, exit_code
from tests.authority_helpers import seed_authority
from tests.test_scheduler import _patch_loop
from tests.test_solis_power_switch import FakeRest, _intent, _now, _window


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "events.db"),
        proactive_mode="on", inverter_power_switch_entity="select.solisac_power_switch",
        timezone="Europe/London", solis_fallback_current_a=20,
    )


@pytest.fixture
def ha(respx_mock, monkeypatch):
    """Real REST client, with the existing register/read-back fake behind HTTP."""
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)
    rig = FakeRest()
    rig.states["sensor.grid"] = "25000"
    rig.states["sensor.soc"] = "50"
    rig.states["sensor.solis_control_timed_charge_current"] = "40"
    monkeypatch.setattr(scheduler, "_run_orchestrator", AsyncMock())
    monkeypatch.setattr(scheduler, "_run_derived_rerive", AsyncMock())

    async def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/states/"):
            entity = path.removeprefix("/api/states/")
            try:
                state = await rig.get_state(entity)
            except KeyError:
                return httpx.Response(404)
            # Deliberately stale SoC, to exercise sustained failure in daemon tests.
            if entity == "sensor.soc":
                state = state.model_copy(update={
                    "last_updated": datetime.now(UTC) - timedelta(hours=1)
                })
            return httpx.Response(200, json=state.model_dump(mode="json"))
        if request.method == "POST" and path.startswith("/api/services/"):
            domain, service = path.removeprefix("/api/services/").split("/")
            await rig.call_service(domain, service, json.loads(request.content))
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.startswith("/api/states/"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    respx_mock.route().mock(side_effect=respond)
    return rig


def daemon_ticks(monkeypatch, ticks):
    """Advance only at the daemon's sleep, not at other consumers of datetime.now."""
    import asyncio

    original_sleep = asyncio.sleep
    stop = _patch_loop(monkeypatch, ticks)
    index = 0

    class Clock:
        @staticmethod
        def now(tz):
            return ticks[min(index, len(ticks) - 1)].replace(
                tzinfo=ZoneInfo("Europe/London")
            ).astimezone(tz)

    async def advance(seconds):
        nonlocal index
        if seconds == 123:
            index += 1
            if index == len(ticks):
                raise stop
        await original_sleep(0)

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(asyncio, "sleep", advance)
    return stop


def hardware_calls(ha: FakeRest):
    return [call for call in ha.calls if call[0] in {"modbus", "select", "switch", "alphaess"}]


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise", "authority_unknown"])
@pytest.mark.parametrize("ownership", list(Ownership))
async def test_solis_ownership_table(settings, ha, mode, ownership):
    context = ExecutionContext()
    store = seed_authority(settings, "optimise" if mode == "authority_unknown" else mode,
                           context=context)
    if mode == "authority_unknown":
        store.store_path.unlink()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        if ownership == Ownership.PROTECTION:
            lines = await device.reconcile_holds(_intent(holds=(_window(-5),)), _now())
        elif ownership == Ownership.CLOCK:
            _, lines = await device.sync_clock()
        else:
            lines = [await device.set_charge_rate(1000)]
    actuates = mode == "optimise" or (mode == "protect" and ownership != Ownership.CHARGING)
    assert bool(hardware_calls(ha)) is actuates
    if mode == "authority_unknown":
        assert all(line.startswith("[REFUSED] authority unknown") for line in lines)
        assert all(line.outcome is ActionOutcome.REFUSED for line in lines)
    elif not actuates:
        assert any(line.startswith("[SIMULATE]") for line in lines)


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise", "authority_unknown"])
async def test_alphaess_charging_table(settings, ha, mode):
    settings = settings.model_copy(update={"devices": [
        settings.devices[0].model_copy(update={"driver": "alphaess"})
    ]})
    context = ExecutionContext()
    store = seed_authority(settings, "optimise" if mode == "authority_unknown" else mode,
                           context=context)
    if mode == "authority_unknown":
        store.store_path.unlink()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        lines = await inverter_device(settings, rest, execution=context).apply(_intent())
    assert bool(hardware_calls(ha)) is (mode == "optimise")
    expected = ActionOutcome.REFUSED if mode == "authority_unknown" else (
        ActionOutcome.VERIFIED if mode == "optimise" else ActionOutcome.SIMULATED
    )
    assert all(line.outcome is expected for line in lines)
    if mode == "authority_unknown":
        assert lines[0].startswith("[REFUSED] authority unknown")
    elif mode != "optimise":
        assert lines[0].startswith("[SIMULATE]")


@pytest.mark.parametrize("driver", ["solis", "alphaess"])
@pytest.mark.parametrize("ownership", list(Ownership))
@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
def test_gate_table_for_every_driver_owner(settings, driver, ownership, mode):
    context = ExecutionContext()
    seed_authority(settings, mode, context=context)
    config = settings.devices[0].model_copy(update={"driver": driver, "control": "supplier"})
    expected = "simulate" if mode == "observe" or (
        mode == "protect" and ownership == Ownership.CHARGING
    ) else "on"
    assert context.gate.mode_for(ownership, config, settings) == expected


async def test_legacy_on_without_daemon_grants_nothing(settings, ha):
    context = ExecutionContext()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        lines = list(await device.apply(_intent()))
        lines += await device.reconcile_holds(_intent(), _now())
        lines += await device.write_safe_state()
        lines += (await device.sync_clock())[1]
    assert not hardware_calls(ha)
    assert all(line.startswith("[REFUSED] authority unknown") for line in lines)
    assert all(line.outcome is ActionOutcome.REFUSED for line in lines)


@pytest.mark.parametrize("missing_epoch", [False, True])
async def test_old_or_missing_planned_epoch_refuses(settings, ha, missing_epoch):
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", epoch=4, context=context)
    assert store.state is not None
    if missing_epoch:
        context.planned_epoch.set(None)
    else:
        store.save(store.state.model_copy(update={"epoch": 5}))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        line = await inverter_device(settings, rest, execution=context).set_charge_rate(1000)
    assert line.startswith("[REFUSED] stale authority")
    assert line.outcome is ActionOutcome.REFUSED
    assert not hardware_calls(ha)


async def test_epoch_is_checked_after_waiting_for_bus(settings, ha):
    import asyncio

    context = ExecutionContext()
    store = seed_authority(settings, "optimise", context=context)
    bus = context.coordinator_for(settings.devices[0], settings)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        async with bus.action(context.gate, Ownership.CHARGING, settings.devices[0], settings):
            with context.planning():
                task = asyncio.create_task(device.set_charge_rate(1000))
            await asyncio.sleep(0)
            assert not task.done()
            assert store.state is not None
            store.save(store.state.model_copy(update={"epoch": 2}))
        line = await task
    assert line.startswith("[REFUSED] stale authority")
    assert not hardware_calls(ha)


async def test_save_failure_blocks_next_write_until_success(settings, ha, monkeypatch):
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", context=context)
    assert store.state is not None
    candidate = store.state.model_copy(update={"epoch": 2})
    with monkeypatch.context() as patch:
        def fail_save(*_):
            raise OSError

        patch.setattr("ha_spark.authority._durable_write", fail_save)
        with pytest.raises(OSError):
            store.save(candidate)
    assert store.save_failed
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        line = await device.set_charge_rate(1000)
        assert line.outcome is ActionOutcome.REFUSED
        assert not hardware_calls(ha)
        store.retry_pending()
        assert not store.save_failed
        with context.planning():
            line = await device.set_charge_rate(1000)
        assert line.outcome is ActionOutcome.VERIFIED
    assert hardware_calls(ha)


async def test_recorded_transition_refuses_every_owner(settings, ha):
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", context=context)
    assert store.state is not None
    store.save(store.state.model_copy(update={"transition": Transition(
        target="observe", started_at=datetime.now(UTC)
    )}))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        lines = [await device.set_charge_rate(1000)]
        lines += await device.reconcile_holds(_intent(), _now())
        lines += (await device.sync_clock())[1]
    assert all(line.startswith("[REFUSED] not owned") for line in lines)
    assert not hardware_calls(ha)


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise", "authority_unknown"])
async def test_operating_mode_publication_and_http_health(settings, ha, mode, tmp_path):
    store = seed_authority(settings, "optimise" if mode == "authority_unknown" else mode, epoch=7)
    if mode == "authority_unknown":
        store.store_path.unlink()
    channel = DaemonChannel(settings)
    channel.authority = store
    state = AppState(settings, tmp_path / "options.json", executor=channel)
    with TestClient(build_app(state)) as client:
        body = client.get("/api/health").json()
    assert body["operating_mode"] == mode
    assert body["status"] == ("fail" if mode == "authority_unknown" else "ok")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        await publish_operating_mode(rest, store)
    requests = [call.request for call in respx.calls
                if call.request.url.path == "/api/states/sensor.ha_spark_operating_mode"]
    payload = json.loads(requests[-1].content)
    assert payload["state"] == mode
    attrs = payload["attributes"]
    assert attrs["epoch"] == 7
    assert attrs["holds"] == []
    assert attrs["fallback"] == {"active": False, "causes": []}
    assert attrs["save_failed"] is False


async def test_live_save_failure_health_is_read_only(settings, ha, tmp_path):
    store = seed_authority(settings, "optimise")
    store.save_failed = True
    before = store.store_path.read_bytes()
    async with DaemonChannel(settings) as channel:
        channel.authority = store
        status = await channel.submit("authority_status")
        assert status.authority["save_failed"]
        assert channel.queue.empty()
        result = await check_operating_mode(settings)
        assert result.status is Status.FAIL
        assert exit_code([result]) == 1
        state = AppState(settings, tmp_path / "options.json", executor=channel)
        with TestClient(build_app(state)) as client:
            assert client.get("/api/health").json()["status"] == "fail"
    assert store.store_path.read_bytes() == before
    assert not hardware_calls(ha)


@pytest.mark.parametrize("store_kind", ["missing", "known", "deleted"])
def test_mode_show_never_initialises_or_writes(settings, tmp_path, monkeypatch, capsys, store_kind):
    if store_kind != "missing":
        store = seed_authority(settings, "protect", epoch=3)
        if store_kind == "deleted":
            store.store_path.unlink()
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    assert cli.main(["mode", "show"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == ("protect" if store_kind == "known" else "authority_unknown")
    assert output["load_status"] == {
        "known": "known", "missing": "uninitialised", "deleted": "authority_unknown"
    }[store_kind]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("legacy_on", [False, True])
async def test_fresh_and_upgrade_daemon_observe_all_paths(
    settings, ha, monkeypatch, caplog, legacy_on
):
    settings = settings.model_copy(update={
        "proactive_mode": "on" if legacy_on else "simulate", "soc_entity": "sensor.soc",
        "grid_power_entity": "sensor.grid", "inverter_clock_dst_sync": True,
        "soc_failure_threshold": 1,
    })
    stop = daemon_ticks(monkeypatch, [
        datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 31),
        datetime(2026, 6, 10, 23, 32),
    ])
    forecast = AsyncMock(return_value=LoadForecast(total_kwh=24, slots=None, source="test"))
    monkeypatch.setattr(sources, "predict_home_load", forecast)
    sample = AsyncMock()
    monkeypatch.setattr(scheduler, "sample_signals", sample)
    fallback = AsyncMock(side_effect=AssertionError("Observe requested fallback"))
    monkeypatch.setattr(SolisDevice, "apply_fallback", fallback)
    safe_state = AsyncMock(side_effect=AssertionError("Observe requested shutdown writes"))
    monkeypatch.setattr(SolisDevice, "write_safe_state", safe_state)
    monkeypatch.setattr(scheduler.DstClockSync, "due", lambda *_: datetime.now(UTC))
    with caplog.at_level("INFO"), pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    result = load_authority(settings)
    assert result.status == "known"
    assert result.state.committed_mode == "observe"
    assert result.state.epoch == 1
    assert not hardware_calls(ha)
    assert "[SIMULATE]" in caplog.text
    assert "would sync inverter clock" in caplog.text
    assert "would set charge current" in caplog.text
    fallback.assert_not_called()
    safe_state.assert_not_called()
    forecast.assert_awaited()
    sample.assert_awaited()
    paths = [call.request.url.path for call in respx.calls if call.request.method == "POST"]
    assert paths.count("/api/states/sensor.ha_spark_soc_integrity") == 3
    assert paths.count("/api/states/sensor.ha_spark_operating_mode") == 3
    assert "/api/states/sensor.ha_spark_plan_status" in paths


async def test_deleted_store_daemon_notifies_once_and_never_writes(settings, ha, monkeypatch):
    settings = settings.model_copy(update={
        "soc_entity": "sensor.soc", "grid_power_entity": "sensor.grid",
        "inverter_clock_dst_sync": True, "soc_failure_threshold": 1,
    })
    monkeypatch.setattr(scheduler.DstClockSync, "due", lambda *_: datetime.now(UTC))
    fallback = AsyncMock(side_effect=AssertionError("Unknown authority requested fallback"))
    monkeypatch.setattr(SolisDevice, "apply_fallback", fallback)
    store = seed_authority(settings, "optimise", epoch=4)
    store.store_path.unlink()
    stop = daemon_ticks(monkeypatch, [
        datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 31),
    ])
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24, slots=None, source="test")
    ))
    monkeypatch.setattr(scheduler, "sample_signals", AsyncMock())
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert not hardware_calls(ha)
    assert not store.store_path.exists()
    notices = [call for call in ha.calls if call[:2] == ("persistent_notification", "create")]
    assert len(notices) == 1
    assert "authority unknown" in notices[0][2]["message"]
    fallback.assert_not_called()
    assert (await check_operating_mode(settings)).status is Status.FAIL


async def test_new_daemon_bumps_epoch_without_legacy_grant(settings, ha, monkeypatch):
    store = seed_authority(settings, "observe", epoch=8)
    stop = daemon_ticks(monkeypatch, [datetime(2026, 6, 10, 23, 30)])
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24, slots=None, source="test")
    ))
    monkeypatch.setattr(scheduler, "sample_signals", AsyncMock())
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    restarted = AuthorityStore(settings)
    assert restarted.state.epoch == store.state.epoch + 1
    assert restarted.state.committed_mode == "observe"
    assert not hardware_calls(ha)


async def test_daemon_retries_failed_epoch_save_and_notifies_once(settings, ha, monkeypatch):
    seed_authority(settings, "optimise", epoch=4)
    settings = settings.model_copy(update={"grid_power_entity": "sensor.grid"})
    original_save = AuthorityStore.save
    attempts = 0

    def flaky_save(self, state):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError
        original_save(self, state)

    monkeypatch.setattr(AuthorityStore, "save", flaky_save)
    stop = daemon_ticks(monkeypatch, [
        datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 31),
    ])
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24, slots=None, source="test")
    ))
    first_tick = []

    async def sample(*_):
        store = scheduler.execution_context().authority
        first_tick.append((store.save_failed, list(hardware_calls(ha))))

    monkeypatch.setattr(scheduler, "sample_signals", sample)
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert first_tick == [(True, [])]
    assert attempts == 3
    assert load_authority(settings).state.epoch == 5
    assert hardware_calls(ha)
    notices = [call for call in ha.calls if call[:2] == ("persistent_notification", "create")]
    assert len(notices) == 1
    assert notices[0][2]["message"] == "authority save failure"
    assert scheduler.execution_context().authority is None


async def test_failed_initialisation_retries_its_own_candidate(settings, monkeypatch):
    from ha_spark import authority

    store = AuthorityStore(settings)
    original_write = authority._durable_write
    calls = 0

    def fail_marker(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError
        original_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(authority, "_durable_write", fail_marker)
        with pytest.raises(OSError):
            store.start_process()
    assert store.save_failed
    assert not store.known
    store.retry_pending()
    assert not store.save_failed
    assert store.known
    assert store.state.epoch == 1
    assert store.state.committed_mode == "observe"


@pytest.mark.parametrize("mode", ["observe", "authority_unknown"])
async def test_safe_state_and_fallback_never_write_without_charging_authority(settings, ha, mode):
    context = ExecutionContext()
    store = seed_authority(settings, "observe", context=context)
    if mode == "authority_unknown":
        store.store_path.unlink()
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        lines = await device.write_safe_state()
        lines += await device.apply_fallback(_intent().soc)
    assert not hardware_calls(ha)
    expected = ActionOutcome.REFUSED if mode == "authority_unknown" else ActionOutcome.SIMULATED
    assert lines and all(line.outcome is expected for line in lines)


async def test_unknown_daemon_reply_is_not_success(settings, ha, monkeypatch):
    import asyncio

    store = seed_authority(settings, "optimise")
    store.store_path.unlink()
    channel = DaemonChannel(settings)
    monkeypatch.setattr(scheduler, "DaemonChannel", lambda _: channel)
    stop = daemon_ticks(monkeypatch, [
        datetime(2026, 6, 10, 23, 30), datetime(2026, 6, 10, 23, 31),
    ])
    monkeypatch.setattr(sources, "predict_home_load", AsyncMock(
        return_value=LoadForecast(total_kwh=24, slots=None, source="test")
    ))
    requests = []

    async def submit(*_):
        requests.append(asyncio.create_task(channel.submit("apply")))

    monkeypatch.setattr(scheduler, "sample_signals", submit)
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    reply = await requests[0]
    assert reply.error == "device action refused"
    assert any(line.outcome is ActionOutcome.REFUSED for line in reply.lines)
    assert not hardware_calls(ha)


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise", "authority_unknown"])
async def test_orchestrator_uses_authority_not_legacy_mode(settings, ha, monkeypatch, mode):
    from datetime import date

    from ha_spark.energy import orchestrator
    from ha_spark.energy.habits import HabitContext

    store = seed_authority(settings, "optimise" if mode == "authority_unknown" else mode)
    if mode == "authority_unknown":
        store.store_path.unlink()
    monkeypatch.setattr(orchestrator, "_gather_context", AsyncMock(return_value=HabitContext(
        target_date=date(2026, 6, 22), predicted_occupancy=0.1, away_active=True,
        learned_away_factor=0.3,
    )))
    decisions = await orchestrator.orchestrate(settings)
    assert decisions
    assert all(d.mode == ("on" if mode == "optimise" else "simulate") for d in decisions)
    assert all(d.outcome == "advisory" for d in decisions)
    assert not hardware_calls(ha)


async def test_refused_is_never_preview_or_recovery_evidence(settings, ha):
    from ha_spark.energy.soc_monitor import SocMonitor
    from tests.test_soc_monitor import _measurement

    seed_authority(settings, "observe")
    refused = ActionLine("[SIMULATE] misleading presentation", ActionOutcome.REFUSED)
    assert not scheduler._preview_accepted((refused,))
    assert not scheduler._preview_accepted(("[REFUSED] authority unknown",))
    assert not scheduler.recovery_verified((refused,))
    monitor = SocMonitor(path=Path(settings.db_path).parent / "monitor.json")
    monitor.record(_measurement(ok=False), failure_threshold=1)
    monitor.request_fallback()
    await scheduler.finish_recovery(settings, monitor, (refused,))
    assert monitor.fallback_active
    assert not monitor.complete_fallback(action_line=refused).fallback_confirmed
    assert not hardware_calls(ha)


async def test_mode_sensor_preserves_authority_details_without_secrets(settings, ha):
    store = seed_authority(settings, "protect", epoch=9)
    now = datetime.now(UTC)
    store.save(store.state.model_copy(update={
        "transition": Transition(target="optimise", started_at=now,
                                 steps=("pending",), failing_actions=("charge window",)),
        "holds": (HoldRecord(cause="ev", started_at=now, device=HoldDevice(
            driver="solis", power_switch="select.power", hub="solis", slave=1,
        )),),
        "fallback": FallbackState(active=True, causes=("SoC integrity",)),
        "requester_kind": "cli",
    }))
    store.save_failed = True
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        await publish_operating_mode(rest, store)
    payload = json.loads(respx.calls[-1].request.content)
    attrs = payload["attributes"]
    assert payload["state"] == "protect"
    assert attrs["transition_target"] == "optimise"
    assert attrs["transition_state"] == "pending"
    assert attrs["failing_actions"] == ["charge window"]
    assert attrs["holds"][0]["cause"] == "ev"
    assert attrs["holds"][0]["confirmed"] is False
    assert attrs["holds"][0]["owned"] is True
    assert attrs["holds"][0]["pending_release"] is False
    assert attrs["holds"][0]["trusted_end"] is None
    assert attrs["fallback"] == {"active": True, "causes": ["SoC integrity"]}
    assert attrs["epoch"] == 9
    assert attrs["requester_kind"] == "cli"
    assert attrs["last_changed_at"]
    assert attrs["save_failed"] is True
    assert str(store.store_path) not in json.dumps(payload)




async def test_uninitialised_store_warns_without_failing_health(settings):
    result = await check_operating_mode(settings)
    assert result.status is Status.WARN
    assert not load_authority(settings).status == "known"


@pytest.mark.parametrize("revoke", ["deleted", "save_failed", "epoch_moved", "disk_diverged"])
@pytest.mark.parametrize("ownership", list(Ownership))
async def test_authority_lost_during_prerequisite_reads_blocks_the_write(
    settings, ha, monkeypatch, revoke, ownership
):
    """The grant is re-checked at the actuating call, not only on action entry."""
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", epoch=4, context=context)
    assert store.state is not None
    original = HomeAssistantRest.get_state
    revoked = False

    async def get_state(self, entity_id):
        nonlocal revoked
        if not revoked:
            revoked = True
            if revoke == "deleted":
                store.store_path.unlink()
            elif revoke == "save_failed":
                store.save_failed = True
            elif revoke == "epoch_moved":
                store.save(store.state.model_copy(update={"epoch": 5}))
            else:
                diverged = store.state.model_copy(update={"committed_mode": "observe"})
                store.store_path.write_text(diverged.model_dump_json(), encoding="utf-8")
        return await original(self, entity_id)

    monkeypatch.setattr(HomeAssistantRest, "get_state", get_state)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        device = inverter_device(settings, rest, execution=context)
        if ownership == Ownership.PROTECTION:
            lines = await device.reconcile_holds(_intent(holds=(_window(-5),)), _now())
        elif ownership == Ownership.CLOCK:
            _, lines = await device.sync_clock()
        else:
            lines = [await device.set_charge_rate(1000)]
    assert revoked
    assert not hardware_calls(ha)
    assert not any(line.outcome in (
        ActionOutcome.VERIFIED, ActionOutcome.UNCHANGED_VERIFIED
    ) for line in lines if isinstance(line, ActionLine))


async def test_valid_but_different_store_on_disk_is_authority_unknown(settings, ha):
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", epoch=4, context=context)
    assert store.state is not None
    replaced = store.state.model_copy(update={"committed_mode": "observe", "epoch": 5})
    store.store_path.write_text(replaced.model_dump_json(), encoding="utf-8")
    assert load_authority(settings).status == "known"
    assert not store.known
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        line = await inverter_device(settings, rest, execution=context).set_charge_rate(1000)
    assert line.startswith("[REFUSED] authority unknown")
    assert not hardware_calls(ha)


async def test_unreadable_daemon_status_fails_health(settings, monkeypatch):
    from ha_spark import health
    from ha_spark.daemon_channel import Reply

    seed_authority(settings, "optimise")
    monkeypatch.setattr(health, "submit_socket", AsyncMock(
        return_value=Reply(error="daemon response unavailable")
    ))
    result = await check_operating_mode(settings)
    assert result.status is Status.FAIL
