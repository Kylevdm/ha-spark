"""Verified schedule restoration before releasing owned protection (#424/#443)."""
from dataclasses import replace
from datetime import timedelta

import pytest

from ha_spark.energy.scheduler import hold_reconcile_intent
from tests.test_durable_holds import _record, _reload
from tests.test_solis_power_switch import (
    _SWITCH,
    FakeRest,
    _device,
    _intent,
    _now,
    _options,
    _reconcile,
    _seed_owned_hold,
)


def _without_refusal(holds):
    return tuple(
        h.model_copy(update={"release_refused_since": None, "release_refused_reason": None})
        for h in holds
    )


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ha_spark.devices.inverters.solis._READ_BACK_DELAY_SECONDS", 0)


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_release_refuses_an_unreadable_schedule(tmp_path, mode):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode=mode)
    _seed_owned_hold(device)
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "unavailable"
    if mode == "optimise":
        rest.states["sensor.solis_control_work_mode_bitfield"] = "0"
    records = device._execution.authority.state.holds
    lines = await _reconcile(device, _intent())
    assert _options(rest) == []
    # #449: the records are kept, marked as waiting on a refused release.
    assert _without_refusal(device._execution.authority.state.holds) == records
    assert device._execution.authority.state.holds[0].release_refused_reason == (
        "schedule_unverified"
    )
    assert any("[REFUSED]" in line and "schedule" in line for line in lines)


async def test_optimise_release_verifies_plan_before_on(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    await _reconcile(device, _intent())
    assert _options(rest) == ["On"]
    assert rest.calls[-1][2]["option"] == "On"
    assert any(call[0] == "modbus" for call in rest.calls[:-1])
    assert not device._execution.authority.state.holds


async def test_fallback_release_verifies_fixed_schedule_not_soc_plan(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, solis_fallback_current_a="35")
    _seed_owned_hold(device)
    device._execution.authority.save_fallback(active=True, cause="soc_integrity")
    # The fallback transition bumps the epoch; the daemon plans under the new one.
    device._execution.planned_epoch.set(device._execution.authority.state.epoch)
    lines = await _reconcile(device, _intent())
    assert _options(rest) == ["On"]
    assert rest.states["sensor.solis_control_timed_charge_current"] == "35.0"
    assert not device._execution.authority.state.holds
    assert any("[APPLIED]" in line for line in lines)


async def test_overlapping_causes_keep_both_records_until_release_verifies(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode="protect")
    store = device._execution.authority
    ev = _record(device, "ev", pending_release=True)
    dispatch = _record(device, "dispatch")
    store.save_holds((ev, dispatch))
    await _reconcile(device, replace(_intent(), hold_trusted=False))
    assert _options(rest) == []
    assert {h.cause for h in store.state.holds} == {"ev", "dispatch"}
    expired = dispatch.model_copy(update={"trusted_end": _now() - timedelta(seconds=1)})
    store.save_holds((ev, expired))
    await _reconcile(device, replace(_intent(), hold_trusted=False))
    assert _options(rest) == ["On"]
    assert not store.state.holds


async def test_unconfirmed_dispatch_retries_off_after_restart_with_stale_evidence(tmp_path):
    rest = FakeRest(switch="On")
    device = _device(rest, tmp_path, authority_mode="protect")
    device._execution.authority.save_holds((_record(device, "dispatch", confirmed=False),))
    device._execution.authority = _reload(device)
    intent = hold_reconcile_intent(replace(_intent(), hold_trusted=False), None)
    assert intent is not None
    await _reconcile(device, intent)
    assert _options(rest) == ["Off"]
    assert device._execution.authority.state.holds[0].confirmed
    assert rest.states[_SWITCH] == "Off"


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_expired_unconfirmed_dispatch_never_writes_on(tmp_path, mode):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode=mode)
    record = _record(device, "dispatch", confirmed=False)
    device._execution.authority.save_holds((record.model_copy(
        update={"trusted_end": _now() - timedelta(minutes=1)}
    ),))
    intent = hold_reconcile_intent(replace(_intent(), hold_trusted=False), None)
    assert intent is not None
    await _reconcile(device, intent)
    assert _options(rest) == []
    assert not any(call[0] == "modbus" for call in rest.calls)


async def test_release_refuses_a_switch_that_is_not_on_or_off(tmp_path):
    rest = FakeRest(switch="unavailable")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    records = device._execution.authority.state.holds
    lines = await _reconcile(device, _intent())
    assert rest.calls == []
    assert _without_refusal(device._execution.authority.state.holds) == records
    assert any("refused" in line and "not on/off" in line for line in lines)


async def test_protect_release_refuses_an_unreadable_resident_current(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode="protect")
    _seed_owned_hold(device)
    rest.states["sensor.solis_control_timed_charge_current"] = "unavailable"
    lines = await _reconcile(device, _intent())
    assert rest.calls == []
    assert device._execution.authority.state.holds
    assert any("[REFUSED]" in line and "schedule" in line for line in lines)


async def test_protect_release_reads_the_resident_schedule_and_writes_only_the_switch(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode="protect")
    _seed_owned_hold(device)
    await _reconcile(device, _intent())
    assert [call[0:2] for call in rest.calls] == [("select", "select_option")]
    assert _options(rest) == ["On"]
    assert not device._execution.authority.state.holds


async def test_failed_on_read_back_keeps_the_record(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode="protect")
    _seed_owned_hold(device)
    original = rest.call_service

    async def stuck(domain, service, data=None):
        result = await original(domain, service, data)
        if (domain, service) == ("select", "select_option"):
            rest.states[_SWITCH] = "Off"
        return result

    rest.call_service = stuck
    lines = await _reconcile(device, _intent())
    assert _options(rest) == ["On"]
    assert device._execution.authority.state.holds
    assert not any(line.startswith("[APPLIED]") for line in lines)


async def test_refused_release_records_since_and_keeps_it_across_refusals(tmp_path):
    rest = FakeRest(switch="unavailable")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    store = device._execution.authority
    await _reconcile(device, _intent())
    (first,) = store.state.holds
    assert first.release_refused_reason == "switch_unreadable"
    assert first.release_refused_since is not None
    assert not first.release_refused_notified
    rest.states[_SWITCH] = "Off"
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "unavailable"
    await _reconcile(device, _intent())
    (second,) = store.state.holds
    assert second.release_refused_since == first.release_refused_since
    assert second.release_refused_reason == "schedule_unverified"
    assert _options(rest) == []


async def test_unreadable_switch_read_marks_the_refusal(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    original = rest.get_state

    async def broken(entity_id):
        if entity_id == _SWITCH:
            raise RuntimeError("HA down")
        return await original(entity_id)

    rest.get_state = broken
    await _reconcile(device, _intent())
    assert _options(rest) == []
    assert device._execution.authority.state.holds[0].release_refused_reason == (
        "switch_unreadable"
    )


async def test_verified_release_clears_the_refusal_with_the_record(tmp_path):
    rest = FakeRest(switch="unavailable")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    await _reconcile(device, _intent())
    assert device._execution.authority.state.holds[0].release_refused_since is not None
    rest.states[_SWITCH] = "Off"
    await _reconcile(device, _intent())
    assert _options(rest) == ["On"]
    assert not device._execution.authority.state.holds


async def test_holding_again_ends_the_refusal_episode_but_keeps_the_notice_flag(tmp_path):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode="protect")
    store = device._execution.authority
    record = _record(device, "dispatch").model_copy(update={
        "release_refused_since": _now() - timedelta(hours=1),
        "release_refused_reason": "switch_unreadable",
        "release_refused_notified": True,
    })
    store.save_holds((record,))
    held = replace(_intent(), holds=((_now() - timedelta(minutes=5), _now() + timedelta(hours=1)),))
    await _reconcile(device, held)
    (kept,) = store.state.holds
    assert kept.release_refused_since is None
    assert kept.release_refused_reason is None
    assert kept.release_refused_notified


def _refusing_device(tmp_path):
    """Optimise, an owned hold to release, and a schedule that never reads back."""
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path)
    _seed_owned_hold(device)
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "unavailable"
    rest.reads = []
    original = rest.get_state

    async def counted(entity_id):
        rest.reads.append(entity_id)
        return await original(entity_id)

    rest.get_state = counted
    return rest, device


def _attempts(rest):
    """Reads beyond the switch's own: a release attempt checks the schedule."""
    return sum(entity != _SWITCH for entity in rest.reads)


async def _refuse_five_times(device, rest, start):
    for minute in range(5):
        attempts = _attempts(rest)
        lines = await device.reconcile_holds(_intent(), start + timedelta(minutes=minute))
        assert _attempts(rest) > attempts
        assert any("[REFUSED]" in line and "hold kept" in line for line in lines)
    return lines


async def test_refused_releases_back_off_after_five(tmp_path, caplog):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    last = await _refuse_five_times(device, rest, start)
    calls, attempts = len(rest.calls), _attempts(rest)
    caplog.clear()
    for minute in range(5, 14):
        lines = await device.reconcile_holds(_intent(), start + timedelta(minutes=minute))
        # The same refusal line as the last attempt: the daemon logs a repeat at DEBUG.
        assert lines == [last[-1]]
    assert len(rest.calls) == calls
    assert _attempts(rest) == attempts
    assert _options(rest) == []
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert device._execution.authority.state.holds
    lines = await device.reconcile_holds(_intent(), start + timedelta(minutes=14))
    assert _attempts(rest) > attempts
    assert any("[REFUSED]" in line for line in lines)


async def test_back_off_never_delays_a_hold_start(tmp_path):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    await _refuse_five_times(device, rest, start)
    rest.states[_SWITCH] = "On"
    at = start + timedelta(minutes=6)
    held = replace(_intent(), holds=((at - timedelta(minutes=1), at + timedelta(hours=1)),))
    await device.reconcile_holds(held, at)
    assert _options(rest) == ["Off"]


async def test_holding_again_resets_the_back_off(tmp_path):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    await _refuse_five_times(device, rest, start)
    at = start + timedelta(minutes=6)
    held = replace(_intent(), holds=((at - timedelta(minutes=1), at + timedelta(hours=1)),))
    await device.reconcile_holds(held, at)
    attempts = _attempts(rest)
    lines = await device.reconcile_holds(_intent(), at + timedelta(minutes=1))
    assert _attempts(rest) > attempts
    assert any("[REFUSED]" in line and "hold kept" in line for line in lines)


async def test_a_verified_release_resets_the_back_off(tmp_path):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    await _refuse_five_times(device, rest, start)
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "23"
    await device.reconcile_holds(_intent(), start + timedelta(minutes=14))
    assert _options(rest) == ["On"]
    assert not device._execution.authority.state.holds
    # A new episode starts from zero: its first refusal doesn't wait.
    rest.states[_SWITCH] = "Off"
    _seed_owned_hold(device)
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "unavailable"
    for minute in (15, 16):
        # Still inside the old interval: a stale count would skip the second.
        attempts = _attempts(rest)
        await device.reconcile_holds(_intent(), start + timedelta(minutes=minute))
        assert _attempts(rest) > attempts


async def test_an_unverified_on_keeps_the_back_off_count(tmp_path):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    await _refuse_five_times(device, rest, start)
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "23"
    original = rest.call_service

    async def on_never_lands(domain, service, data=None):
        if (domain, service) == ("select", "select_option"):
            rest.calls.append((domain, service, data or {}))
            return []
        return await original(domain, service, data)

    rest.call_service = on_never_lands
    await device.reconcile_holds(_intent(), start + timedelta(minutes=14))
    assert _options(rest) == ["On"]
    assert device._execution.authority.state.holds
    rest.call_service = original
    rest.states["sensor.solis_control_timed_charge_start_hours"] = "unavailable"
    await device.reconcile_holds(_intent(), start + timedelta(minutes=15))
    # The sixth refusal in the episode: backed off again at once.
    attempts = _attempts(rest)
    await device.reconcile_holds(_intent(), start + timedelta(minutes=16))
    assert _attempts(rest) == attempts


async def test_back_off_keeps_the_refusal_reason_current(tmp_path):
    rest, device = _refusing_device(tmp_path)
    start = _now()
    await _refuse_five_times(device, rest, start)
    rest.states[_SWITCH] = "unavailable"
    await device.reconcile_holds(_intent(), start + timedelta(minutes=5))
    (held,) = device._execution.authority.state.holds
    assert held.release_refused_reason == "switch_unreadable"
    rest.states[_SWITCH] = "Off"
    await device.reconcile_holds(_intent(), start + timedelta(minutes=6))
    (kept,) = device._execution.authority.state.holds
    assert kept.release_refused_reason == "schedule_unverified"
    assert kept.release_refused_since == held.release_refused_since
