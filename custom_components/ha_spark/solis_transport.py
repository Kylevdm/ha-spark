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
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from itertools import count
from typing import Literal, Protocol

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.framer import FramerType
from pymodbus.pdu import ModbusPDU

_LOGGER = logging.getLogger(__name__)

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
    @property
    def connection_epoch(self) -> int: ...

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
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        with _wire_context():
            self._client = client_factory(host, port)
        self.label: str = "gateway"
        self._wall_clock = wall_clock
        self._up_since: float | None = None
        self._down_since: float | None = None
        # Bounded even during a pathological flapping storm.
        self._drops: deque[float] = deque(maxlen=100_000)
        self._last_drop_reason: str | None = None
        self._last_drop_at: str | None = None
        self._log_times: dict[int, deque[float]] = {
            logging.WARNING: deque(maxlen=10), logging.INFO: deque(maxlen=10),
        }
        self._suppressed = {logging.WARNING: 0, logging.INFO: 0}
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

    @property
    def connected(self) -> bool:
        """Read-only connectivity; never a write permission."""
        return not self._closed and self._client.connected

    @property
    def connection_epoch(self) -> int:
        """Changes after every successful connection, invalidating validation."""
        return self._connection

    def _prune_drops(self, now: float) -> None:
        while self._drops and self._drops[0] <= now - 86400:
            self._drops.popleft()

    def link_stats(self) -> dict[str, int | str | bool | None]:
        """Non-identifying link history; independent of write permissions."""
        self._prune_drops(self._wall_clock())
        return {
            "drops_24h": len(self._drops),
            "last_drop_reason": self._last_drop_reason,
            "last_drop_at": self._last_drop_at,
            "connected": self.connected,
        }

    def _link_log(self, level: int, now: float, message: str, *args: object) -> None:
        times = self._log_times[level]
        while times and times[0] <= now - 3600:
            times.popleft()
        if len(times) >= 10:
            self._suppressed[level] += 1
            _LOGGER.debug(message, *args)
        else:
            times.append(now)
            suppressed = self._suppressed[level]
            self._suppressed[level] = 0
            _LOGGER.log(level, message + " suppressed=%d", *args, suppressed)

    def _link_drop(
        self, reason: TransportError, op: str | None, address: int | None,
    ) -> None:
        # Observability must never interfere with bus I/O, even if a clock or
        # logging handler fails. Only the first observation of a drop counts.
        with suppress(Exception):
            if self._up_since is None or self._closed:
                return
            now = self._monotonic()
            uptime = now - self._up_since
            self._up_since = None
            self._down_since = now
            wall_now = self._wall_clock()
            self._prune_drops(wall_now)
            self._drops.append(wall_now)
            self._last_drop_reason = reason.value
            self._last_drop_at = datetime.fromtimestamp(wall_now, UTC).isoformat()
            self._link_log(
                logging.WARNING, now,
                "Native link %s dropped reason=%s op=%s address=%s uptime_s=%.3f",
                self.label, reason.value, op, address, uptime,
            )

    def _link_up(self) -> None:
        with suppress(Exception):
            now = self._monotonic()
            self._up_since = now
            down_since = self._down_since
            self._down_since = None
            if down_since is not None:
                self._link_log(
                    logging.INFO, now, "Native link %s reconnected downtime_s=%.3f",
                    self.label, now - down_since,
                )

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
                self._link_up()

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
                self._link_drop(TransportError.DISCONNECTED, op, address)
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
                self._link_drop(TransportError.DISCONNECTED, "write", address)
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
                        self._link_drop(TransportError.DISCONNECTED, request.op, request.address)
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
            self._link_drop(error, request.op, request.address)
        else:
            with suppress(Exception):
                if not self._client.connected:
                    self._link_drop(
                        error or TransportError.DISCONNECTED, request.op, request.address,
                    )
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
