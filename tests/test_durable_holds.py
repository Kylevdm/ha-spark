"""Durable protection ownership (#421)."""
from datetime import timedelta

import pytest

from ha_spark.authority import AuthorityStore
from tests.test_solis_power_switch import (
    _SWITCH,
    FakeRest,
    _device,
    _intent,
    _now,
    _options,
    _reconcile,
    _window,
)


def _reload(device):
    store = device._execution.authority
    settings = device._settings.model_copy(update={
        "ha_url": "http://ha.test", "ha_token": "test",
        "db_path": str(store.store_path.parent / "events.db"),
    })
    return AuthorityStore(settings)


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_owned_hold_saved_before_write_and_confirmed(tmp_path, mode):
    rest = FakeRest()
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    original = rest.call_service

    async def check(domain, service, data=None):
        assert store.state.holds[0].owned
        assert not store.state.holds[0].confirmed
        return await original(domain, service, data)

    rest.call_service = check
    await _reconcile(device, _intent(holds=(_window(-10),)))
    assert store.state.holds[0].confirmed
    loaded = _reload(device)
    assert loaded.state.holds == store.state.holds
    assert loaded.state.holds[0].trusted_end > _now()


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_owner_off_is_not_owned_or_released(tmp_path, mode):
    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode=mode)
    await _reconcile(device, _intent(holds=(_window(-10),)))
    lines = await _reconcile(device, _intent())
    assert not device._execution.authority.state.holds
    assert _options(rest) == []
    assert any("[SKIP]" in line and "owner" in line for line in lines)


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_failed_hold_save_writes_nothing(tmp_path, monkeypatch, mode):
    rest = FakeRest()
    device = _device(rest, tmp_path, authority_mode=mode)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("ha_spark.authority._durable_write", fail)
    await _reconcile(device, _intent(holds=(_window(-10),)))
    assert _options(rest) == []
    assert device._execution.authority.save_failed


@pytest.mark.parametrize("value", [4, 1441])
def test_degraded_option_bounds(value):
    from ha_spark.config import Settings
    with pytest.raises(ValueError):
        Settings(hold_degraded_minutes=value)


def test_ev_record_rejects_dispatch_end():
    from datetime import UTC, datetime

    from ha_spark.authority import HoldDevice, HoldRecord

    with pytest.raises(ValueError, match="trusted_end"):
        HoldRecord(
            cause="ev", started_at=datetime.now(UTC), trusted_end=datetime.now(UTC),
            device=HoldDevice(driver="solis", power_switch="select.power", hub="solis", slave=1),
        )


def test_master_empty_holds_load(tmp_path):
    device = _device(FakeRest(), tmp_path)
    assert _reload(device).state.holds == ()


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_unconfirmed_record_reread_after_restart(tmp_path, mode):
    rest = FakeRest()
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    original = rest.call_service

    async def crash(domain, service, data=None):
        await original(domain, service, data)
        raise RuntimeError("crash after write")

    rest.call_service = crash
    intent = _intent(holds=(_window(-10),))
    await _reconcile(device, intent)
    assert not store.state.holds[0].confirmed
    loaded = _reload(device)
    device._execution.authority = loaded
    await _reconcile(device, intent)
    assert loaded.state.holds[0].confirmed
    assert _options(rest) == ["Off"]
    assert loaded.state.holds[0].trusted_end > _now() - timedelta(seconds=1)


def test_unchanged_holds_save_is_a_no_op_even_without_known_authority(tmp_path) -> None:
    from ha_spark.authority import AuthorityStore
    from ha_spark.config import Settings

    store = AuthorityStore(Settings(ha_url="http://ha.test", ha_token="t", data_dir=str(tmp_path)))
    store.initialise()
    store.save_failed = True
    store.save_holds(())  # unchanged: nothing to persist, nothing raised
    assert store.save_failed is True


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_already_off_switch_never_opens_a_new_owned_cause(tmp_path, mode):
    """F4: confirming our request by read never creates a cause we didn't write."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from ha_spark.authority import HoldDevice, HoldRecord

    rest = FakeRest(switch="Off")  # the owner turned it Off after our write failed
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    store.save_holds((HoldRecord(
        cause="dispatch", started_at=datetime.now(UTC) - timedelta(minutes=5),
        trusted_end=datetime.now(UTC) + timedelta(hours=1),
        device=HoldDevice(
            driver="solis", power_switch=_SWITCH, hub=device._settings.solis_control_hub,
            slave=device._settings.solis_modbus_slave,
        ),
    ),))
    await _reconcile(device, replace(_intent(holds=(_window(-5),)), ev_hold_active=True))
    assert [h.cause for h in store.state.holds] == ["dispatch"]
    assert store.state.holds[0].confirmed  # our request, re-verified by read
    assert _options(rest) == []


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_records_dropped_when_switch_already_reads_on(tmp_path, mode):
    """F3: an ended hold whose switch already reads On is cleared without a write."""
    rest = FakeRest()
    device = _device(rest, tmp_path, authority_mode=mode)
    await _reconcile(device, _intent(holds=(_window(-10),)))
    assert device._execution.authority.state.holds
    rest.states[_SWITCH] = "On"  # released elsewhere (e.g. the relinquish safe state)
    await _reconcile(device, _intent())
    assert device._execution.authority.state.holds == ()
    assert _options(rest) == ["Off"]


def _record(device, cause, *, confirmed=True, pending_release=False):
    from datetime import UTC, datetime

    from ha_spark.authority import HoldDevice, HoldRecord

    return HoldRecord(
        cause=cause, started_at=datetime.now(UTC) - timedelta(minutes=5),
        trusted_end=datetime.now(UTC) + timedelta(hours=1) if cause == "dispatch" else None,
        confirmed=confirmed, pending_release=pending_release,
        device=HoldDevice(
            driver="solis", power_switch=_SWITCH, hub=device._settings.solis_control_hub,
            slave=device._settings.solis_modbus_slave,
        ),
    )


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_ev_hold_starting_under_our_dispatch_hold_is_recorded(tmp_path, mode):
    """A second cause under ha-spark's own Off survives the first one's release."""
    from dataclasses import replace

    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    store.save_holds((_record(device, "dispatch"),))
    await _reconcile(device, replace(_intent(holds=(_window(-5),)), ev_hold_active=True))
    assert sorted(h.cause for h in store.state.holds) == ["dispatch", "ev"]
    assert all(h.confirmed for h in store.state.holds)
    assert _options(rest) == []


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_dispatch_starting_under_our_ev_hold_is_recorded(tmp_path, mode):
    from dataclasses import replace

    rest = FakeRest(switch="Off")
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    store.save_holds((_record(device, "ev", pending_release=True),))
    await _reconcile(device, replace(_intent(holds=(_window(-5),)), ev_hold_active=False))
    [dispatch] = [h for h in store.state.holds if h.cause == "dispatch"]
    assert dispatch.confirmed and dispatch.trusted_end > _now()
    assert _options(rest) == []


@pytest.mark.parametrize("mode", ["protect", "optimise"])
async def test_an_unconfirmed_request_never_releases_an_owner_off(tmp_path, mode):
    """Our Off write failed before reaching HA; the owner switched Off since."""
    rest = FakeRest()
    device = _device(rest, tmp_path, authority_mode=mode)
    store = device._execution.authority
    original = rest.call_service

    async def unreachable(domain, service, data=None):
        raise RuntimeError("HA unreachable")

    rest.call_service = unreachable
    await _reconcile(device, _intent(holds=(_window(-10),)))
    assert not store.state.holds[0].confirmed
    rest.call_service = original
    rest.states[_SWITCH] = "Off"  # the owner's own Off
    lines = await _reconcile(device, _intent())
    assert _options(rest) == []
    assert any("[SKIP]" in line and "owner" in line for line in lines)
