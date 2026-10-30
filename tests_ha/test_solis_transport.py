"""Bus transport behaviour without sockets or a real gateway."""

import asyncio
import logging
from collections.abc import Sequence

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.framer import FramerType
from pymodbus.pdu import ExceptionResponse, ModbusPDU
from pymodbus.pdu.register_message import (
    ReadHoldingRegistersResponse,
    ReadInputRegistersResponse,
    WriteMultipleRegistersResponse,
)

from custom_components.ha_spark.solis_transport import PymodbusTcpTransport, TransportError


class WireTimeout(ModbusIOException):
    def __init__(self, message: str) -> None:
        Exception.__init__(self, message)


class WireDisconnected(ConnectionException):
    def __init__(self, message: str) -> None:
        Exception.__init__(self, message)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class Client:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.connected = False
        self.connections: list[bool] = [True]
        self.calls: list[tuple[str, int, int, float]] = []
        self.reply: ModbusPDU | Exception | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def connect(self) -> bool:
        self.connected = self.connections.pop(0) if self.connections else True
        return self.connected

    def close(self) -> None:
        self.connected = False

    async def exchange(self, op: str, address: int, device_id: int) -> ModbusPDU:
        self.calls.append((op, address, device_id, self.clock.now))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.clock.now += 0.04
        if isinstance(self.reply, Exception):
            raise self.reply
        if self.reply is not None:
            return self.reply
        if op == "write":
            return WriteMultipleRegistersResponse(address=address, count=1)
        if op == "input":
            return ReadInputRegistersResponse(registers=[42])
        return ReadHoldingRegistersResponse(registers=[42])

    async def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusPDU:
        return await self.exchange("input", address, device_id)

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> ModbusPDU:
        return await self.exchange("holding", address, device_id)

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> ModbusPDU:
        return await self.exchange("write", address, device_id)


def make_transport(client: Client, clock: Clock) -> PymodbusTcpTransport:
    return PymodbusTcpTransport(
        "unused", client_factory=lambda host, port: client,
        monotonic=clock.monotonic, sleep=clock.sleep,
    )


async def test_pacing_elapsed_and_unit_ids() -> None:
    clock = Clock()
    client = Client(clock)
    transport = make_transport(client, clock)
    try:
        first = await transport.read_input(33000, 1, device_id=2)
        second = await transport.write(43141, [10], device_id=3)
        third = await transport.read_holding(43141, 1)
        assert first.registers == (42,)
        assert second.ok
        assert third.registers == (42,)
        assert first.elapsed_s == pytest.approx(0.04)
        assert second.elapsed_s == pytest.approx(0.04)
        assert client.calls == [
            ("input", 33000, 2, 0.0), ("write", 43141, 3, 0.35),
            ("holding", 43141, 1, 0.7),
        ]
    finally:
        await transport.close()


async def test_pending_writes_precede_reads_fifo() -> None:
    clock = Clock()
    client = Client(clock)
    client.release = asyncio.Event()
    transport = make_transport(client, clock)
    first = asyncio.create_task(transport.read_input(33000, 1))
    try:
        await client.entered.wait()
        jobs = [asyncio.create_task(transport.read_holding(43141, 1)),
                asyncio.create_task(transport.write(43142, [1])),
                asyncio.create_task(transport.write(43143, [2]))]
        await asyncio.sleep(0)
        client.release.set()
        await asyncio.gather(first, *jobs)
        assert [call[1] for call in client.calls] == [33000, 43142, 43143, 43141]
    finally:
        client.release.set()
        await transport.close()


@pytest.mark.parametrize(("reply", "error", "code"), [
    (TimeoutError("secret"), TransportError.TIMEOUT, None),
    (WireTimeout("secret"), TransportError.TIMEOUT, None),
    (WireDisconnected("secret"), TransportError.DISCONNECTED, None),
    (RuntimeError("secret"), TransportError.BAD_RESPONSE, None),
    (ExceptionResponse(4, 2), TransportError.EXCEPTION, 2),
    (ReadInputRegistersResponse(registers=[]), TransportError.BAD_RESPONSE, None),
    (ReadInputRegistersResponse(registers=[65536]), TransportError.BAD_RESPONSE, None),
    (ReadHoldingRegistersResponse(registers=[42]), TransportError.BAD_RESPONSE, None),
])
async def test_fixed_errors(reply: ModbusPDU | Exception, error: TransportError,
                            code: int | None) -> None:
    clock = Clock()
    client = Client(clock)
    client.reply = reply
    transport = make_transport(client, clock)
    try:
        result = await transport.read_input(33000, 1)
        assert result.error == error
        assert result.exception_code == code
        assert "secret" not in repr(result)
        assert result.elapsed_s == pytest.approx(0.04)
    finally:
        await transport.close()


async def test_reconnect_backoff_and_disconnected_write_not_replayed() -> None:
    clock = Clock()
    client = Client(clock)
    client.connections = [False, False, False, True]
    transport = make_transport(client, clock)
    try:
        result = await transport.read_input(33000, 1)
        assert result.error == TransportError.DISCONNECTED
        refused = await transport.write(43141, [10])
        assert refused.error == TransportError.DISCONNECTED
        for _ in range(20):
            await asyncio.sleep(0)
        assert clock.sleeps[:3] == [1.0, 2.0, 4.0]
        assert (await transport.read_holding(43141, 1)).registers == (42,)
        assert all(call[0] != "write" for call in client.calls)
    finally:
        await transport.close()


@pytest.mark.parametrize(("address", "count"), [(33000, 0), (33000, 126), (-1, 1),
                                               (65535, 2), (33000, True)])
async def test_invalid_read_never_sent(address: int, count: int) -> None:
    clock = Clock()
    client = Client(clock)
    transport = make_transport(client, clock)
    try:
        assert (await transport.read_input(address, count)).error == TransportError.BAD_RESPONSE
        assert not client.calls
    finally:
        await transport.close()


@pytest.mark.parametrize("values", [[], [1] * 124, [-1], [65536], [True]])
async def test_invalid_write_never_sent(values: Sequence[int]) -> None:
    clock = Clock()
    client = Client(clock)
    transport = make_transport(client, clock)
    try:
        assert (await transport.write(43141, values)).error == TransportError.BAD_RESPONSE
        assert not client.calls
    finally:
        await transport.close()


async def test_close_resolves_pending_work_and_never_reopens() -> None:
    clock = Clock()
    client = Client(clock)
    client.release = asyncio.Event()
    transport = make_transport(client, clock)
    first = asyncio.create_task(transport.read_input(33000, 1))
    await client.entered.wait()
    pending = asyncio.create_task(transport.read_holding(43141, 1))
    await asyncio.sleep(0)
    await transport.close()
    assert (await first).error == TransportError.DISCONNECTED
    assert (await pending).error == TransportError.DISCONNECTED
    assert (await transport.read_input(33000, 1)).error == TransportError.DISCONNECTED
    assert not client.connected


@pytest.mark.parametrize("reply", [
    WriteMultipleRegistersResponse(address=43142, count=1),
    WriteMultipleRegistersResponse(address=43141, count=2),
    ReadHoldingRegistersResponse(registers=[1]),
])
async def test_write_requires_matching_ack(reply: ModbusPDU) -> None:
    clock = Clock()
    client = Client(clock)
    transport = make_transport(client, clock)
    try:
        await transport.read_input(33000, 1)
        client.reply = reply
        assert (await transport.write(43141, [10])).error == TransportError.BAD_RESPONSE
    finally:
        await transport.close()


async def test_queued_write_is_not_replayed_after_reconnect() -> None:
    clock = Clock()
    client = Client(clock)
    client.release = asyncio.Event()
    transport = make_transport(client, clock)
    first = asyncio.create_task(transport.read_input(33000, 1))
    try:
        await client.entered.wait()
        queued = asyncio.create_task(transport.write(43141, [10]))
        await asyncio.sleep(0)
        client.reply = WireTimeout("secret")
        client.release.set()
        assert (await first).error == TransportError.TIMEOUT
        assert (await queued).error == TransportError.DISCONNECTED
        client.reply = None
        for _ in range(10):
            await asyncio.sleep(0)
        assert (await transport.read_input(33000, 1)).registers == (42,)
        assert all(call[0] != "write" for call in client.calls)
    finally:
        client.release.set()
        await transport.close()


async def test_default_client_mbap_no_unpaced_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    client = Client(clock)
    factory_calls: list[tuple[str, int, FramerType, int, int]] = []

    def factory(host: str, *, port: int, framer: FramerType, retries: int,
                reconnect_delay: int) -> Client:
        factory_calls.append((host, port, framer, retries, reconnect_delay))
        return client

    monkeypatch.setattr("custom_components.ha_spark.solis_transport.AsyncModbusTcpClient", factory)
    transport = PymodbusTcpTransport("gateway", 1502, monotonic=clock.monotonic, sleep=clock.sleep)
    try:
        await transport.read_input(33000, 1, device_id=1)
        await transport.read_input(35000, 1, device_id=2)
        assert factory_calls == [("gateway", 1502, FramerType.SOCKET, 0, 0)]
    finally:
        await transport.close()


async def test_library_logs_are_scoped_and_do_not_leak_hosts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()

    class LoggingClient(Client):
        async def connect(self) -> bool:
            logging.getLogger("pymodbus.logging").warning("secret-gateway")
            return await super().connect()

        async def exchange(self, op: str, address: int, device_id: int) -> ModbusPDU:
            logging.getLogger("pymodbus.logging").warning("secret-exception")
            return await super().exchange(op, address, device_id)

    transport = make_transport(LoggingClient(clock), clock)
    try:
        await transport.read_input(33000, 1)
        assert "secret" not in caplog.text
        logging.getLogger("pymodbus.logging").warning("unrelated client")
        assert "unrelated client" in caplog.text
    finally:
        await transport.close()


async def test_backoff_caps_resets_and_recovers_idle_disconnect() -> None:
    clock = Clock()
    delays: asyncio.Queue[float] = asyncio.Queue()
    permits: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(seconds: float) -> None:
        if seconds < 1:  # inter-frame pacing; only backoff delays are gated
            clock.now += seconds
            await asyncio.sleep(0)
            return
        delays.put_nowait(seconds)
        await permits.get()
        clock.now += seconds

    client = Client(clock)
    client.connections = [False] * 8 + [True]
    transport = PymodbusTcpTransport(
        "unused", client_factory=lambda host, port: client,
        monotonic=clock.monotonic, sleep=sleep,
    )
    try:
        assert (await transport.read_input(33000, 1)).error == TransportError.DISCONNECTED
        for expected in [1, 2, 4, 8, 16, 32, 60, 60]:
            assert await delays.get() == expected
            permits.put_nowait(None)
        await asyncio.sleep(0)
        assert client.connected
        assert (await transport.read_input(33000, 1)).registers == (42,)
        # The socket can disappear while the worker is idle, not just on send.
        client.connected = False
        assert (await transport.write(43141, [1])).error == TransportError.DISCONNECTED
        assert await delays.get() == 1
        permits.put_nowait(None)
        await asyncio.sleep(0)
        assert client.connected
    finally:
        await transport.close()


async def test_cancelled_queued_write_never_sends() -> None:
    clock = Clock()
    client = Client(clock)
    client.release = asyncio.Event()
    transport = make_transport(client, clock)
    first = asyncio.create_task(transport.read_input(33000, 1))
    try:
        await client.entered.wait()
        queued = asyncio.create_task(transport.write(43141, [10]))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        client.release.set()
        await first
        await transport.read_holding(43141, 1)
        assert all(call[0] != "write" for call in client.calls)
    finally:
        client.release.set()
        await transport.close()


async def test_write_queued_during_pacing_gap_goes_before_waiting_read() -> None:
    clock = Clock()
    client = Client(clock)
    gap_started = asyncio.Event()
    gap_done = asyncio.Event()

    async def sleep(seconds: float) -> None:
        gap_started.set()
        await gap_done.wait()
        clock.now += seconds

    transport = PymodbusTcpTransport(
        "unused", client_factory=lambda host, port: client,
        monotonic=clock.monotonic, sleep=sleep,
    )
    try:
        await transport.read_input(33000, 1)
        await asyncio.wait_for(gap_started.wait(), 1)  # paced while idle
        read = asyncio.create_task(transport.read_holding(43141, 1))
        await asyncio.sleep(0)
        write = asyncio.create_task(transport.write(43142, [1]))
        await asyncio.sleep(0)
        gap_done.set()
        await asyncio.gather(read, write)
        assert [call[1] for call in client.calls] == [33000, 43142, 43141]
        assert client.calls[1][3] >= 0.04 + 0.31
    finally:
        gap_done.set()
        await transport.close()
