"""In-memory Solis registers for native-module seam tests; never opens a socket.

Delays are simulated, not slept. A delayed write lands after K subsequent valid
transport calls (reads or writes), or advance(K). Caching retains the first
successful answer for each exact instruction, including its unit id, until
caching is disabled. Drift is applied immediately after a write lands. Faults
are consumed per address first, then from the global next-write queue.
"""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from custom_components.ha_spark.solis_transport import (
    ReadResult,
    TransportError,
    WriteResult,
    valid_request,
    valid_words,
)

Bank = Literal["input", "holding"]


@dataclass(frozen=True)
class WriteFault:
    kind: str
    exception_code: int = 2
    delay_calls: int = 1

    def __post_init__(self) -> None:
        if self.kind not in ("exception", "timeout", "silent_noop", "delayed_apply"):
            raise ValueError("invalid_fault")
        if not 1 <= self.exception_code <= 255 or self.delay_calls < 1:
            raise ValueError("invalid_fault")


@dataclass(frozen=True)
class Traffic:
    op: Literal["read", "write"]
    bank: Bank
    address: int
    payload: int | tuple[int, ...]
    device_id: int


@dataclass
class _DelayedWrite:
    remaining: int
    address: int
    values: tuple[int, ...]


class FakeSolisDevice:
    """Mutable register banks and deterministic gateway/firmware fault controls."""

    def __init__(self) -> None:
        self.input: dict[int, int] = {}
        self.holding: dict[int, int] = {}
        self.elapsed_s = 0.04
        self.elapsed_overrides: deque[float] = deque()
        self.cached_elapsed_s = 0.001
        self.connected = True
        self.traffic: list[Traffic] = []
        self.drift: dict[int, int] = {}
        self.spurious_zeros: set[tuple[Bank, int, int]] = set()
        self._caching = False
        self._cache: dict[tuple[Bank, int, int, int], tuple[int, ...]] = {}
        self._faults: deque[WriteFault] = deque()
        self._address_faults: dict[int, deque[WriteFault]] = {}
        self._delayed: list[_DelayedWrite] = []
        self.set_identity()

    @property
    def caching(self) -> bool:
        return self._caching

    @caching.setter
    def caching(self, enabled: bool) -> None:
        self._caching = enabled
        if not enabled:
            self._cache.clear()

    def set_input(self, address: int, values: Sequence[int]) -> None:
        self._set(self.input, address, values)

    def set_holding(self, address: int, values: Sequence[int]) -> None:
        self._set(self.holding, address, values)

    @staticmethod
    def _set(bank: dict[int, int], address: int, values: Sequence[int]) -> None:
        if not valid_request(address, len(values), 65536, 1) or not valid_words(values):
            raise ValueError("invalid_registers")
        bank.update((address + offset, value) for offset, value in enumerate(values))

    def set_identity(
        self, *, model: int = 0x0026, inverter_type: int = 0x2031,
        tou: int = 0, dispatch: int = 0,
    ) -> None:
        self.set_input(33000, [model] + [0] * 19)
        self.set_input(35000, [inverter_type])
        self.set_input(33289, [tou])
        self.set_input(34502, [dispatch])

    def set_read_frames(self) -> None:
        """Populate all #575 polling frames without touching identity or its serial."""
        from custom_components.ha_spark.solis_frames import FRAMES

        for frame in FRAMES:
            setter = self.set_input if frame.bank == "input" else self.set_holding
            setter(frame.address, [0] * frame.count)
        self.set_input(33022, [26, 10, 30, 12, 0, 0])

    def inject_write_fault(
        self, fault: WriteFault, *, address: int | None = None, times: int = 1
    ) -> None:
        if times < 1:
            raise ValueError("invalid_fault_count")
        queue = (
            self._faults if address is None else self._address_faults.setdefault(address, deque())
        )
        queue.extend([fault] * times)

    def advance(self, calls: int = 1) -> None:
        for pending in self._delayed[:]:
            pending.remaining -= calls
            if pending.remaining <= 0:
                self._apply(pending.address, pending.values)
                self._delayed.remove(pending)

    def _apply(self, address: int, values: tuple[int, ...]) -> None:
        self.set_holding(address, values)
        for register in range(address, address + len(values)):
            if register in self.drift:
                self.set_holding(register, [self.drift[register]])

    def _elapsed(self, cached: bool = False) -> float:
        if self.elapsed_overrides:
            return self.elapsed_overrides.popleft()
        return self.cached_elapsed_s if cached else self.elapsed_s

    async def read_input(self, address: int, count: int, *, device_id: int = 1) -> ReadResult:
        return self._read("input", address, count, device_id)

    async def read_holding(self, address: int, count: int, *, device_id: int = 1) -> ReadResult:
        return self._read("holding", address, count, device_id)

    def _read(self, bank: Bank, address: int, count: int, device_id: int) -> ReadResult:
        if not valid_request(address, count, 125, device_id):
            return ReadResult(error=TransportError.BAD_RESPONSE)
        if not self.connected:
            return ReadResult(error=TransportError.DISCONNECTED)
        self.advance()
        self.traffic.append(Traffic("read", bank, address, count, device_id))
        key = (bank, address, count, device_id)
        cached = self.caching and key in self._cache
        elapsed = self._elapsed(cached)
        source = self.input if bank == "input" else self.holding
        if (bank, address, count) in self.spurious_zeros:
            self.spurious_zeros.remove((bank, address, count))
            return ReadResult(elapsed, (0,) * count)
        if cached:
            return ReadResult(elapsed, self._cache[key])
        if any(register not in source for register in range(address, address + count)):
            return ReadResult(elapsed, error=TransportError.EXCEPTION, exception_code=2)
        words = tuple(source[register] for register in range(address, address + count))
        if self.caching:
            self._cache[key] = words
        return ReadResult(elapsed, words)

    async def write(
        self, address: int, values: Sequence[int], *, device_id: int = 1
    ) -> WriteResult:
        words = tuple(values)
        if not valid_request(address, len(words), 123, device_id) or not valid_words(words):
            return WriteResult(error=TransportError.BAD_RESPONSE)
        if not self.connected:
            return WriteResult(error=TransportError.DISCONNECTED)
        self.advance()
        bank: Bank = "input" if address < 40000 else "holding"
        self.traffic.append(Traffic("write", bank, address, words, device_id))
        elapsed = self._elapsed()
        if bank == "input":
            return WriteResult(elapsed, error=TransportError.EXCEPTION, exception_code=2)
        queue = self._address_faults.get(address) or self._faults
        fault = queue.popleft() if queue else None
        if fault is not None:
            if fault.kind == "exception":
                return WriteResult(
                    elapsed, error=TransportError.EXCEPTION, exception_code=fault.exception_code,
                )
            if fault.kind == "timeout":
                return WriteResult(elapsed, error=TransportError.TIMEOUT)
            if fault.kind == "delayed_apply":
                self._delayed.append(_DelayedWrite(fault.delay_calls, address, words))
            return WriteResult(elapsed, ok=True)
        self._apply(address, words)
        return WriteResult(elapsed, ok=True)

    async def close(self) -> None:
        self.connected = False
