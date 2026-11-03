"""Fault controls exposed by the in-memory Solis register device."""

from collections.abc import Sequence

import pytest

from custom_components.ha_spark.solis_transport import Transport, TransportError
from tests_ha.fake_solis import FakeSolisDevice, WriteFault


async def test_identity_and_independent_register_banks() -> None:
    device = FakeSolisDevice()
    transport: Transport = device
    assert (await transport.read_input(33000, 20)).registers == (0x26,) + (0,) * 19
    for address, word in [(35000, 0x2031), (33289, 0), (34502, 0)]:
        assert (await transport.read_input(address, 1)).registers == (word,)
    device.set_identity(model=0x99, inverter_type=0x1234, tou=0xAA55, dispatch=0xAA55)
    assert (await transport.read_input(33289, 1)).registers == (0xAA55,)
    device.set_input(43141, [55])
    device.set_holding(43141, [10, 20])
    assert (await transport.read_holding(43141, 2)).registers == (10, 20)
    assert (await transport.read_input(43141, 1)).registers == (55,)
    assert (await transport.read_holding(40000, 1)).exception_code == 2
    assert (await transport.write(33000, [1])).exception_code == 2
    assert (await transport.read_input(33000, 1)).registers == (0x99,)
    await transport.close()
    assert (await transport.write(43141, [1])).error == TransportError.DISCONNECTED


async def test_cached_repeats_are_fast_stale_and_instruction_specific() -> None:
    device = FakeSolisDevice()
    device.caching = True
    device.set_holding(43141, [10, 20])
    prime = await device.read_holding(43141, 1)
    device.set_holding(43141, [30])
    for _ in range(5):
        cached = await device.read_holding(43141, 1)
        assert cached.registers == (10,)
        assert cached.elapsed_s < 0.005
    assert prime.elapsed_s == 0.04
    assert (await device.read_holding(43141, 2)).registers == (30, 20)
    device.caching = False
    assert (await device.read_holding(43141, 1)).registers == (30,)
    assert (await device.read_holding(43141, 1)).elapsed_s == 0.04


async def test_simulated_elapsed_and_traffic() -> None:
    device = FakeSolisDevice()
    device.elapsed_s = 0.06
    device.elapsed_overrides.extend([0.1, 0.2])
    assert (await device.read_input(33000, 1, device_id=2)).elapsed_s == 0.1
    assert (await device.write(43141, [5])).elapsed_s == 0.2
    assert (await device.read_holding(43141, 1)).elapsed_s == 0.06
    assert [(t.op, t.bank, t.address, t.payload, t.device_id) for t in device.traffic] == [
        ("read", "input", 33000, 1, 2),
        ("write", "holding", 43141, (5,), 1),
        ("read", "holding", 43141, 1, 1),
    ]


@pytest.mark.parametrize(("kind", "error", "code"), [
    ("exception", TransportError.EXCEPTION, 3),
    ("timeout", TransportError.TIMEOUT, None),
    ("silent_noop", None, None),
])
async def test_write_faults_leave_bank_unchanged(kind: str, error: TransportError | None,
                                               code: int | None) -> None:
    device = FakeSolisDevice()
    device.set_holding(43141, [10])
    device.inject_write_fault(WriteFault(kind, exception_code=3), times=2)
    for _ in range(2):
        result = await device.write(43141, [20])
        assert result.error == error
        assert result.exception_code == code
        assert result.ok == (error is None)
        assert (await device.read_holding(43141, 1)).registers == (10,)
    assert (await device.write(43141, [20])).ok
    assert (await device.read_holding(43141, 1)).registers == (20,)


async def test_address_scoped_fault_and_delayed_apply() -> None:
    device = FakeSolisDevice()
    device.set_holding(43141, [10, 11])
    device.inject_write_fault(WriteFault("delayed_apply", delay_calls=2), address=43141)
    assert (await device.write(43142, [22])).ok
    assert (await device.write(43141, [20])).ok
    assert (await device.read_holding(43141, 2)).registers == (10, 22)
    assert (await device.read_holding(43141, 1)).registers == (20,)
    device.inject_write_fault(WriteFault("delayed_apply", delay_calls=3))
    await device.write(43141, [30])
    device.advance(3)
    assert (await device.read_holding(43141, 1)).registers == (30,)


async def test_drift_and_one_shot_spurious_zero_frame() -> None:
    device = FakeSolisDevice()
    device.drift[43141] = 99
    assert (await device.write(43141, [10, 20])).ok
    device.spurious_zeros.add(("holding", 43141, 2))
    assert (await device.read_holding(43141, 2)).registers == (0, 0)
    assert (await device.read_holding(43141, 2)).registers == (99, 20)
    device.spurious_zeros.add(("input", 33000, 1))
    assert (await device.read_input(33000, 1)).registers == (0,)
    assert (await device.read_input(33000, 1)).registers == (0x26,)


@pytest.mark.parametrize("values", [[], [1] * 124, [-1], [65536], [True]])
async def test_fake_validates_words_like_wire(values: Sequence[int]) -> None:
    device = FakeSolisDevice()
    assert (await device.write(43141, values)).error == TransportError.BAD_RESPONSE
    assert not device.traffic


@pytest.mark.parametrize("count", [0, 126, True])
async def test_fake_validates_reads_like_wire(count: int) -> None:
    device = FakeSolisDevice()
    assert (await device.read_input(33000, count)).error == TransportError.BAD_RESPONSE
    assert not device.traffic
