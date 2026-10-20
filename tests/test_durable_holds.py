"""Durable protection ownership (#421)."""
from datetime import timedelta

import pytest

from ha_spark.authority import AuthorityStore
from tests.test_solis_power_switch import (
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
