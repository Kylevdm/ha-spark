"""The shared power-switch seam uses direct fake-bus evidence on every edge."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest

from custom_components.ha_spark import native_power
from custom_components.ha_spark.native import NativeDevices
from custom_components.ha_spark.solis_frames import Reading
from custom_components.ha_spark.topology import Inverter
from tests_ha.fake_solis import FakeSolisDevice, WriteFault

INV = Inverter(id="inv", bank_id="bank", driver_kind="solis", transport="native",
               gateway_host="gateway.invalid")
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def device(fake: FakeSolisDevice) -> NativeDevices:
    readings: dict[str, dict[str, Reading]] = {"inv": {}}
    return cast(NativeDevices, SimpleNamespace(
        transport=lambda inv: fake, readings=readings,
        reading=lambda inv, key: readings[inv.id].get(key),
    ))


@pytest.mark.parametrize(("words", "expected"), [
    (None, None), ([], None), ([190], "on"), ([222], "off"),
    ([0], None), ([190, 222], None), ([True], None),
])
def test_power_decode(words: list[int] | None, expected: str | None) -> None:
    assert native_power.decode_power(words) == expected


@pytest.mark.parametrize("age", [-1, 0, 60, 61])
def test_power_polled_freshness(age: int) -> None:
    native = device(FakeSolisDevice())
    native.readings["inv"]["holding_43007"] = Reading(
        NOW - timedelta(seconds=age), (190,), 0)
    assert native_power.polled_power(native, INV, NOW) == ("on" if 0 <= age <= 60 else None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(seconds: float) -> None:
        pass
    monkeypatch.setattr(native_power, "verify_sleep", sleep)


async def test_power_direct_pre_read_write_and_verified_cache() -> None:
    fake = FakeSolisDevice()
    fake.set_holding(43007, [190])
    native = device(fake)
    result = await native_power.async_write_power_locked(native, INV, "off", recheck=lambda: None)
    assert result.outcome == "applied"
    assert (result.before, result.after, result.mismatch) == ("on", "off", False)
    assert [(t.op, t.address) for t in fake.traffic] == [
        ("read", 43007), ("write", 43007), ("read", 43007)]
    assert native.readings["inv"]["holding_43007"].registers == (222,)


@pytest.mark.parametrize("target", ["on", "off"])
async def test_power_refusal_after_pre_read(target: native_power.PowerState) -> None:
    fake = FakeSolisDevice()
    fake.set_holding(43007, [222 if target == "on" else 190])
    result = await native_power.async_write_power_locked(
        device(fake), INV, target, recheck=lambda: "authority_unknown")
    assert result.outcome == "refused" and result.reason == "authority_unknown"
    assert not any(t.op == "write" for t in fake.traffic)


async def test_power_never_blind_on_but_off_can_close() -> None:
    fake = FakeSolisDevice()
    native = device(fake)
    assert (await native_power.async_write_power_locked(
        native, INV, "on", recheck=lambda: None)).outcome == "unreadable"
    assert not any(t.op == "write" for t in fake.traffic)
    assert (await native_power.async_write_power_locked(
        native, INV, "off", recheck=lambda: None)).outcome == "applied"


@pytest.mark.parametrize(("fault", "expected"), [("silent_noop", "unverified"),
                                                 ("exception", "failed")])
async def test_power_fault_is_not_verified(fault: str, expected: str) -> None:
    fake = FakeSolisDevice()
    fake.set_holding(43007, [190])
    fake.inject_write_fault(WriteFault(fault), address=43007)
    result = await native_power.async_write_power_locked(
        device(fake), INV, "off", recheck=lambda: None)
    assert result.outcome == expected
    assert result.mismatch is (expected == "unverified")


async def test_power_matching_state_never_writes() -> None:
    fake = FakeSolisDevice()
    fake.set_holding(43007, [222])
    result = await native_power.async_write_power_locked(
        device(fake), INV, "off", recheck=lambda: None)
    assert result.outcome == "unchanged"
    assert not any(t.op == "write" for t in fake.traffic)
