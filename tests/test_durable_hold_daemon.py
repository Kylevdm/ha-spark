"""Restart reconciliation through the daemon tick, with HA fixtures only."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord
from ha_spark.config import Settings
from ha_spark.devices.execution import execution_context
from ha_spark.energy import scheduler
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from tests.authority_helpers import seed_authority
from tests.test_authority_gate import daemon_ticks
from tests.test_scheduler import (
    _EV_STATUS_URL,
    _INTENT,
    _dispatch_state,
    _ev_status_state,
    _mock_power_switch,
    _plan,
    _restore_schedule_confirmed,
)

NOW = datetime(2026, 6, 10, 21, 0, tzinfo=UTC)


def episode(tmp_path, mode, cause="ev", confirmed=True):
    settings = Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "events.db"),
        ev_status_entity="sensor.ev", inverter_power_switch_entity="select.solisac_power_switch",
        dispatch_entity="binary_sensor.dispatch", hold_degraded_minutes=5,
        notify_service="test", timezone="Europe/London",
    )
    store = seed_authority(settings, mode)
    record = HoldRecord(
        cause=cause, started_at=NOW - timedelta(minutes=20), confirmed=confirmed,
        trusted_end=NOW + timedelta(minutes=30) if cause == "dispatch" else None,
        device=HoldDevice(
            driver="solis", power_switch="select.solisac_power_switch",
            hub=settings.solis_control_hub, slave=settings.solis_modbus_slave,
        ),
    )
    store.save_holds((record,))
    respx.get(url__regex=r"http://ha.test/api/states/sensor.solis_control_.*").mock(
        return_value=httpx.Response(404),
    )
    return settings, store


def stale_evidence():
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(
        "Paused", at=NOW - timedelta(hours=2),
    ))
    respx.get("http://ha.test/api/states/binary_sensor.dispatch").mock(
        return_value=_dispatch_state("off", at=NOW - timedelta(hours=2)),
    )


@pytest.mark.parametrize("mode", ["protect", "optimise"])
@pytest.mark.parametrize("cause", ["ev", "dispatch"])
@pytest.mark.parametrize("confirmed", [False, True])
@respx.mock
async def test_restart_reconciles_before_soc_fallback_or_plan(
    tmp_path, monkeypatch, mode, cause, confirmed,
):
    settings, original = episode(tmp_path, mode, cause, confirmed=confirmed)
    switch = _mock_power_switch(monkeypatch, initial="Off")
    stale_evidence()
    events = []
    reconcile = scheduler.reconcile_tick

    async def checked_reconcile(*args, **kwargs):
        result = await reconcile(*args, **kwargs)
        store = execution_context().authority
        assert store.state.holds[0].confirmed
        assert switch["option"] == "Off"
        paths = [call.request.url.path for call in respx.calls]
        assert paths.index("/api/states/select.solisac_power_switch") < paths.index(
            "/api/states/sensor.ev"
        )
        events.append("reconcile")
        return result

    async def soc_tick(*args):
        assert events == ["reconcile"]
        events.append("soc/fallback")
        return SocMeasurement(SocStatus.OK, NOW, value=50)

    async def plan(*args, **kwargs):
        assert events[:2] == ["reconcile", "soc/fallback"]
        events.append("plan")
        p = _plan(replace(_INTENT, holds=(), hold_trusted=False))
        return scheduler.RunResult(p, p)

    monkeypatch.setattr(scheduler, "reconcile_tick", checked_reconcile)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", soc_tick)
    monkeypatch.setattr(scheduler, "run_once", plan)
    stop = daemon_ticks(monkeypatch, [NOW.astimezone(ZoneInfo("Europe/London"))])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert events[:3] == ["reconcile", "soc/fallback", "plan"]
    assert AuthorityStore(settings).state.store_id == original.state.store_id


@pytest.mark.parametrize("mode", ["protect", "optimise"])
@respx.mock
async def test_ev_clearance_sequence_survives_restart_and_stale_resets_it(
    tmp_path, monkeypatch, mode,
):
    settings, store = episode(tmp_path, mode)
    switch = _mock_power_switch(monkeypatch, initial="Off")
    _restore_schedule_confirmed(monkeypatch)
    reports = {"at": NOW}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _: _ev_status_state(
        "Paused", at=reports["at"],
    ))
    plan = _plan(replace(_INTENT, holds=()))
    for minute in range(2):
        reports["at"] = NOW + timedelta(minutes=minute)
        await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert store.state.holds[0].clear_count == 2
    restarted = AuthorityStore(settings)
    restarted.start_process()
    execution_context().authority = restarted
    execution_context().planned_epoch.set(restarted.state.epoch)
    reports["at"] = NOW + timedelta(minutes=2)
    await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert restarted.state.holds[0].clear_count == 3
    # Repeated GETs do not count; stale reports reset persisted progress.
    await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert restarted.state.holds[0].clear_count == 3
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=10))
    assert restarted.state.holds[0].clear_count == 0
    for minute in range(11, 16):
        reports["at"] = NOW + timedelta(minutes=minute)
        await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert switch["option"] == "On"
    assert restarted.state.holds == ()


@respx.mock
async def test_stale_report_rearms_a_refused_ev_release(tmp_path, monkeypatch):
    """#424/Q5: a pending release needs fresh clear evidence; stale re-arms the hold."""
    settings, store = episode(tmp_path, "protect")
    switch = _mock_power_switch(monkeypatch, initial="Off")
    reports = {"at": NOW}
    respx.get(_EV_STATUS_URL).mock(side_effect=lambda _: _ev_status_state(
        "Paused", at=reports["at"],
    ))
    plan = _plan(replace(_INTENT, holds=()))
    # The resident schedule is unreadable (404), so the release is refused.
    for minute in range(5):
        reports["at"] = NOW + timedelta(minutes=minute)
        await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert store.state.holds[0].pending_release
    assert switch["option"] == "Off"
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=30))
    record = store.state.holds[0]
    assert not record.pending_release and record.clear_count == 0
    # Now restorable, one fresh clear report is not enough to release.
    _restore_schedule_confirmed(monkeypatch)
    reports["at"] = NOW + timedelta(minutes=31)
    await scheduler.reconcile_tick(settings, plan, reports["at"])
    assert switch["option"] == "Off"
    assert store.state.holds[0].clear_count == 1


@pytest.mark.parametrize("confirmed", [False, True])
@respx.mock
async def test_recorded_dispatch_retries_off_on_stale_dispatch(tmp_path, monkeypatch, confirmed):
    """#443: an unconfirmed request retries its Off too; read-back confirms it."""
    settings, store = episode(tmp_path, "protect", "dispatch", confirmed=confirmed)
    switch = _mock_power_switch(monkeypatch, initial="On")
    stale_evidence()
    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=(), hold_trusted=False)), NOW,
    )
    assert switch["option"] == "Off"
    assert store.state.holds[0].confirmed



@respx.mock
async def test_degraded_notice_persisted_once_and_rearmed(tmp_path, monkeypatch):
    settings, store = episode(tmp_path, "protect")
    _mock_power_switch(monkeypatch, initial="Off")
    stale_evidence()
    notices = []

    async def notify(*args):
        notices.append(args)

    monkeypatch.setattr(scheduler, "send_notification", notify)
    plan = _plan(replace(_INTENT, holds=(), hold_trusted=False))
    await scheduler.reconcile_tick(settings, plan, NOW)
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=6))
    assert len(notices) == 1
    assert store.state.holds[0].degraded_notified
    restarted = AuthorityStore(settings)
    execution_context().authority = restarted
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=7))
    assert len(notices) == 1
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state(
        "Charging", at=NOW + timedelta(minutes=8),
    ))
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=8))
    assert not restarted.state.holds[0].degraded_notified
    stale_evidence()
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=9))
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=15))
    assert len(notices) == 2


@respx.mock
async def test_stale_dispatch_never_extends_recorded_end(tmp_path, monkeypatch):
    settings, store = episode(tmp_path, "protect", "dispatch")
    end = store.state.holds[0].trusted_end
    _mock_power_switch(monkeypatch, initial="Off")
    stale_evidence()
    respx.get("http://ha.test/api/states/binary_sensor.dispatch").mock(
        return_value=_dispatch_state("on", [{
            "start": NOW.isoformat(), "end": (NOW + timedelta(hours=4)).isoformat(),
        }], at=NOW - timedelta(hours=2)),
    )
    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=(), hold_trusted=False)), NOW,
    )
    assert store.state.holds[0].trusted_end == end
    # No trusted update is needed for a recorded dispatch to reach its end.
    switch_writes = respx.post("http://ha.test/api/services/select/select_option")
    await scheduler.reconcile_tick(
        settings, _plan(replace(_INTENT, holds=(), hold_trusted=False)), end,
    )
    assert any(b'"On"' in call.request.content for call in switch_writes.calls)


@pytest.mark.parametrize("mode", ["protect", "optimise"])
@respx.mock
async def test_daemon_reload_keeps_durable_ev_clearance(tmp_path, monkeypatch, mode):
    settings, store = episode(tmp_path, mode)
    record = store.state.holds[0].model_copy(update={
        "clear_count": 2, "last_clear_report_at": NOW - timedelta(minutes=1),
    })
    store.save_holds((record,))
    _mock_power_switch(monkeypatch, initial="Off")
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused", at=NOW))
    respx.get("http://ha.test/api/states/binary_sensor.dispatch").mock(
        return_value=_dispatch_state("off", at=NOW),
    )
    states = []
    original_state = scheduler.AppState

    def app_state(**kwargs):
        state = original_state(**kwargs)
        states.append(state)
        return state

    seen = []

    async def plan(*args, **kwargs):
        state = kwargs["ev_hold_state"]
        seen.append(state)
        assert state.clear_count == 3
        assert execution_context().authority.state.holds[0].clear_count == 3
        states[0].settings = settings.model_copy(update={"hold_degraded_minutes": 10})
        p = _plan(replace(_INTENT, holds=()))
        return scheduler.RunResult(p, p)

    async def soc(*args):
        return SocMeasurement(SocStatus.OK, NOW, value=50)

    monkeypatch.setattr(scheduler, "AppState", app_state)
    monkeypatch.setattr(scheduler, "run_once", plan)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", soc)
    stop = daemon_ticks(monkeypatch, [
        NOW.astimezone(ZoneInfo("Europe/London")),
        (NOW + timedelta(minutes=30)).astimezone(ZoneInfo("Europe/London")),
    ])
    with pytest.raises(stop):
        await scheduler.run_forever(settings, poll_seconds=123)
    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert AuthorityStore(settings).state.holds[0].clear_count == 3


@respx.mock
async def test_degraded_notice_trusts_the_pass_dispatch_picture(tmp_path, monkeypatch):
    """F2: a dispatch hold whose picture this pass trusts is never degraded."""
    settings, store = episode(tmp_path, "protect", cause="dispatch")
    settings = settings.model_copy(update={"dispatch_entity": ""})
    _mock_power_switch(monkeypatch, initial="Off")
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused", at=NOW))
    notices = []

    async def notify(*args):
        notices.append(args)

    monkeypatch.setattr(scheduler, "send_notification", notify)
    window = (NOW - timedelta(minutes=20), NOW + timedelta(minutes=30))
    plan = _plan(replace(_INTENT, holds=(window,)))
    for minute in (0, 6, 12):
        await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=minute))
    assert notices == []


@respx.mock
async def test_ev_clearance_updates_every_ev_record(tmp_path, monkeypatch):
    """F1: a record under an older device identity follows the same clearance."""
    settings, store = episode(tmp_path, "protect")
    old = store.state.holds[0]
    store.save_holds((old, old.model_copy(update={
        "device": old.device.model_copy(update={"power_switch": "select.old_switch"}),
    })))
    monkeypatch.setattr(execution_context(), "authority", store)
    state = scheduler.EvHoldState()
    for minute in range(5):
        evidence = scheduler.EvidenceFreshness(
            "fresh", NOW + timedelta(minutes=minute), 0.0, 300.0,
            NOW + timedelta(minutes=minute),
        )
        await scheduler._observe_ev_hold(
            state, False, evidence, now=NOW + timedelta(minutes=minute), device=object(),
            battery_freshness=None, battery_direction_value="unknown",
        )
    assert not state.active
    assert all(h.pending_release for h in store.state.holds)


def fresh_evidence(at):
    respx.get(_EV_STATUS_URL).mock(return_value=_ev_status_state("Paused", at=at))
    respx.get("http://ha.test/api/states/binary_sensor.dispatch").mock(
        return_value=_dispatch_state("off", at=at),
    )


def ended_dispatch_hold(tmp_path):
    settings, store = episode(tmp_path, "protect", cause="dispatch")
    store.save_holds((store.state.holds[0].model_copy(
        update={"trusted_end": NOW - timedelta(minutes=1)}
    ),))
    return settings, store


@respx.mock
async def test_refused_release_notifies_once_across_restart(tmp_path, monkeypatch):
    settings, store = ended_dispatch_hold(tmp_path)
    switch = _mock_power_switch(monkeypatch, initial="Off")
    fresh_evidence(NOW)
    notices = []

    async def notify(*args):
        notices.append(args)

    monkeypatch.setattr(scheduler, "send_notification", notify)
    plan = _plan(replace(_INTENT, holds=(), hold_trusted=True))
    await scheduler.reconcile_tick(settings, plan, NOW)
    (held,) = store.state.holds
    assert held.release_refused_reason == "schedule_unverified"
    assert switch["option"] == "Off"
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=3))
    assert notices == []
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=6))
    assert len(notices) == 1
    assert "schedule to restore is not verified" in notices[0][3]
    restarted = AuthorityStore(settings)
    execution_context().authority = restarted
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=7))
    assert len(notices) == 1
    assert restarted.state.holds[0].release_refused_notified


@respx.mock
async def test_refused_release_notice_failure_never_blocks(tmp_path, monkeypatch):
    settings, store = ended_dispatch_hold(tmp_path)
    _mock_power_switch(monkeypatch, initial="Off")
    fresh_evidence(NOW)

    async def broken(*args):
        raise RuntimeError("notify down")

    monkeypatch.setattr(scheduler, "send_notification", broken)
    plan = _plan(replace(_INTENT, holds=(), hold_trusted=True))
    await scheduler.reconcile_tick(settings, plan, NOW)
    await scheduler.reconcile_tick(settings, plan, NOW + timedelta(minutes=6))
    assert store.state.holds[0].release_refused_notified
