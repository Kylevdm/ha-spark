"""Native Solis bus I/O, without HA or actuation policy.

One instance owns one gateway (host, port); callers share it across unit ids.
Documented register numbers are PDU addresses, not offsets. A single worker
serialises frames, prioritising queued writes over polling reads. Timing covers
only the exchange, excluding connection, queue wait and inter-frame pacing.
No write is retried or retained across a disconnect. Authority, ownership claims,
read-back verification and register semantics belong above this boundary.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from typing import Literal, Protocol

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.framer import FramerType
from pymodbus.pdu import ModbusPDU

_IN_TRANSPORT = ContextVar("solis_transport_io", default=False)


class _WireLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # pymodbus logs raw socket exceptions and frames. Suppress only our
        # I/O context, including the connection callbacks inheriting it.
        return not _IN_TRANSPORT.get()


logging.getLogger("pymodbus.logging").addFilter(_WireLogFilter())


@contextmanager
def _wire_context() -> Iterator[None]:
    token = _IN_TRANSPORT.set(True)
    try:
        yield
    finally:
        _IN_TRANSPORT.reset(token)


class TransportError(StrEnum):
    """Closed, non-sensitive outcomes; never carry exception text."""

    DISCONNECTED = "disconnected"
    TIMEOUT = "timeout"
    EXCEPTION = "exception"
    BAD_RESPONSE = "bad_response"


@dataclass(frozen=True)
class ReadResult:
    elapsed_s: float = 0.0
    registers: tuple[int, ...] = ()
    error: TransportError | None = None
    exception_code: int | None = None


@dataclass(frozen=True)
class WriteResult:
    elapsed_s: float = 0.0
    ok: bool = False
    error: TransportError | None = None
    exception_code: int | None = None


class Transport(Protocol):
    async def read_input(self, address: int, count: int, *, device_id: int = 1) -> ReadResult: ...

    async def read_holding(self, address: int, count: int, *, device_id: int = 1) -> ReadResult: ...

    async def write(
        self, address: int, values: Sequence[int], *, device_id: int = 1
    ) -> WriteResult: ...

    async def close(self) -> None: ...


class ModbusClient(Protocol):
    """Typed injection seam matching the pinned pymodbus client."""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> bool: ...

    def close(self) -> None: ...

    def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> Awaitable[ModbusPDU]: ...

    def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> Awaitable[ModbusPDU]: ...

    def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> Awaitable[ModbusPDU]: ...


def _client(host: str, port: int) -> ModbusClient:
    # pymodbus calls MBAP/TCP framing SOCKET. Disable its unpaced retries and
    # reconnect: this transport is the sole owner of pacing and backoff.
    return AsyncModbusTcpClient(
        host, port=port, framer=FramerType.SOCKET, retries=0, reconnect_delay=0,
    )


def valid_request(address: int, size: int, maximum: int, device_id: int) -> bool:
    """Reject malformed requests before either a fake or a wire sees them."""
    return (
        type(address) is int and type(size) is int and type(device_id) is int
        and 0 <= address <= 65535 and 1 <= size <= maximum
        and address + size <= 65536 and 1 <= device_id <= 247
    )


def valid_words(values: Sequence[int]) -> bool:
    return all(type(value) is int and 0 <= value <= 65535 for value in values)


@dataclass
class _Request:
    op: Literal["input", "holding", "write"]
    address: int
    size: int
    values: tuple[int, ...]
    device_id: int
    result: asyncio.Future[ReadResult | WriteResult]
    connection: int

    def failure(self, error: TransportError) -> ReadResult | WriteResult:
        if self.op == "write":
            return WriteResult(error=error)
        return ReadResult(error=error)


class PymodbusTcpTransport:
    """Lazy connection with a FIFO priority queue and 1–60 s reconnect backoff.

    The first read attempts connection. Writes while disconnected (including
    before that first read) fail immediately. Reads during reconnection also
    fail immediately; the polling caller can try again after recovery. close()
    is terminal and resolves queued/in-flight callers without replaying writes.
    """

    def __init__(
        self, host: str, port: int = 502, *,
        client_factory: Callable[[str, int], ModbusClient] = _client,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        with _wire_context():
            self._client = client_factory(host, port)
        self._monotonic = monotonic
        self._sleep = sleep
        self._queue: asyncio.PriorityQueue[tuple[int, int, _Request]] = asyncio.PriorityQueue()
        self._sequence = count()
        self._worker: asyncio.Task[None] | None = None
        self._reconnector: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._reconnect_needed = asyncio.Event()
        self._closed = False
        self._last_end: float | None = None
        self._connection = 0

    async def _start(self) -> None:
        async with self._start_lock:
            if self._worker is not None or self._closed:
                return
            await self._connect()
            if self._closed:
                return
            self._worker = asyncio.create_task(self._run())
            self._reconnector = asyncio.create_task(self._reconnect())

    async def _connect(self) -> None:
        with _wire_context():
            try:
                connected = await self._client.connect()
            except Exception:
                connected = False
            if not connected:
                self._client.close()
                self._reconnect_needed.set()
            else:
                self._connection += 1
                self._reconnect_needed.clear()

    async def _reconnect(self) -> None:
        while True:
            await self._reconnect_needed.wait()
            delay = 1.0
            while not self._client.connected:
                await self._sleep(delay)
                await self._connect()
                delay = min(delay * 2, 60.0)
            self._reconnect_needed.clear()

    async def read_input(self, address: int, count: int, *, device_id: int = 1) -> ReadResult:
        return await self._read("input", address, count, device_id)

    async def read_holding(self, address: int, count: int, *, device_id: int = 1) -> ReadResult:
        return await self._read("holding", address, count, device_id)

    async def _read(
        self, op: Literal["input", "holding"], address: int, size: int, device_id: int
    ) -> ReadResult:
        if not valid_request(address, size, 125, device_id):
            return ReadResult(error=TransportError.BAD_RESPONSE)
        await self._start()
        if self._closed or not self._client.connected:
            if not self._closed:
                self._reconnect_needed.set()
            return ReadResult(error=TransportError.DISCONNECTED)
        result = await self._submit(op, address, size, (), device_id)
        assert isinstance(result, ReadResult)
        return result

    async def write(
        self, address: int, values: Sequence[int], *, device_id: int = 1
    ) -> WriteResult:
        words = tuple(values)
        if not valid_request(address, len(words), 123, device_id) or not valid_words(words):
            return WriteResult(error=TransportError.BAD_RESPONSE)
        if self._closed or not self._client.connected:
            if not self._closed and self._worker is not None:
                self._reconnect_needed.set()
            return WriteResult(error=TransportError.DISCONNECTED)
        result = await self._submit("write", address, len(words), words, device_id)
        assert isinstance(result, WriteResult)
        return result

    async def _submit(
        self, op: Literal["input", "holding", "write"], address: int, size: int,
        values: tuple[int, ...], device_id: int,
    ) -> ReadResult | WriteResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ReadResult | WriteResult] = loop.create_future()
        request = _Request(op, address, size, values, device_id, future, self._connection)
        self._queue.put_nowait((0 if op == "write" else 1, next(self._sequence), request))
        return await future

    async def _run(self) -> None:
        while True:
            # Pace before dequeuing, so a write queued during the gap still goes
            # ahead of a read that was already waiting.
            if self._last_end is not None:
                gap = self._last_end + 0.310 - self._monotonic()
                if gap > 0:
                    await self._sleep(gap)
            _, _, request = await self._queue.get()
            try:
                if request.result.cancelled():
                    continue
                if not self._client.connected or request.connection != self._connection:
                    if not self._client.connected:
                        self._reconnect_needed.set()
                    result = request.failure(TransportError.DISCONNECTED)
                else:
                    with _wire_context():
                        result = await self._exchange(request)
                if not request.result.done():
                    request.result.set_result(result)
            finally:
                if not request.result.done():
                    request.result.set_result(request.failure(TransportError.DISCONNECTED))
                self._queue.task_done()

    async def _exchange(self, request: _Request) -> ReadResult | WriteResult:
        started = self._monotonic()
        error: TransportError | None = None
        code: int | None = None
        registers: tuple[int, ...] = ()
        try:
            if request.op == "write":
                reply = await self._client.write_registers(
                    request.address, list(request.values), device_id=request.device_id,
                )
            elif request.op == "input":
                reply = await self._client.read_input_registers(
                    request.address, count=request.size, device_id=request.device_id,
                )
            else:
                reply = await self._client.read_holding_registers(
                    request.address, count=request.size, device_id=request.device_id,
                )
            if reply.isError():
                if type(reply.exception_code) is int and 1 <= reply.exception_code <= 255:
                    error, code = TransportError.EXCEPTION, reply.exception_code
                else:
                    error = TransportError.BAD_RESPONSE
            elif request.op == "write":
                if (reply.function_code != 16 or reply.address != request.address
                        or reply.count != request.size):
                    error = TransportError.BAD_RESPONSE
            elif (reply.function_code != (4 if request.op == "input" else 3)
                  or len(reply.registers) != request.size or not valid_words(reply.registers)):
                error = TransportError.BAD_RESPONSE
            else:
                registers = tuple(reply.registers)
        except ConnectionException:
            error = TransportError.DISCONNECTED
        except (TimeoutError, ModbusIOException):
            error = TransportError.TIMEOUT
        except Exception:
            error = TransportError.BAD_RESPONSE
        finally:
            self._last_end = self._monotonic()
        elapsed = self._last_end - started
        if error in (TransportError.DISCONNECTED, TransportError.TIMEOUT):
            self._client.close()
            self._reconnect_needed.set()
        if request.op == "write":
            return WriteResult(elapsed, error is None, error, code)
        return ReadResult(elapsed, registers, error, code)

    async def close(self) -> None:
        self._closed = True
        # Wait for initial connect, if any, before closing the same client.
        async with self._start_lock:
            tasks = [task for task in (self._worker, self._reconnector) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with _wire_context():
                self._client.close()
            while not self._queue.empty():
                _, _, request = self._queue.get_nowait()
                if not request.result.done():
                    request.result.set_result(request.failure(TransportError.DISCONNECTED))
                self._queue.task_done()
