"""Process-shared permission seam and indivisible, reentrant inverter bus actions.

The daemon binds the sole authority store. Unbound callers fail closed. Planned
epochs are checked under the bus lock before prerequisite reads and read-back.
Factories and direct driver construction use the same default context. Coordinators
keep loop-local locks (CLI/tests may run successive event loops); the daemon uses
one event loop, and no lock is held across loops or shared with another process.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, Protocol, TypeVar
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from ha_spark.authority import AuthorityStore
    from ha_spark.config import DeviceConfig, Settings


class Ownership(StrEnum):
    PROTECTION = "protection"
    CHARGING = "charging"
    CLOCK = "clock"


class WriteGate:
    """The only permission implementation; legacy keys grant nothing."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def mode_for(self, ownership: Ownership, config: DeviceConfig, settings: Settings) -> str:
        store = self.context.authority
        if store is None or store.save_failed or not store.known:
            return "refuse:authority unknown"
        state = store.state
        assert state is not None
        if state.transition is not None:
            return "refuse:not owned"
        if self.context.planned_epoch.get() != state.epoch:
            return "refuse:stale authority"
        if state.committed_mode == "observe":
            return "simulate"
        if state.committed_mode == "protect" and ownership == Ownership.CHARGING:
            return "simulate"
        return "on"


@dataclass
class _BusState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task[Any] | None = None
    modes: list[str] = field(default_factory=list)
    decisions: dict[tuple[Ownership, int, int], str] = field(default_factory=dict)


class BusCoordinator:
    """Serialise complete actions; nested helpers reuse the task's decision."""

    def __init__(self) -> None:
        self._loops: WeakKeyDictionary[asyncio.AbstractEventLoop, _BusState] = WeakKeyDictionary()

    def _state(self) -> _BusState:
        loop = asyncio.get_running_loop()
        if loop not in self._loops:
            self._loops[loop] = _BusState()
        return self._loops[loop]

    @property
    def mode(self) -> str:
        state = self._state()
        if state.owner is not asyncio.current_task() or not state.modes:
            raise RuntimeError("Device permission requested outside a held action")
        return state.modes[-1]

    @asynccontextmanager
    async def action(
        self, gate: WriteGate, ownership: Ownership, config: DeviceConfig, settings: Settings
    ) -> AsyncIterator[None]:
        state = self._state()
        task = asyncio.current_task()
        outer = state.owner is not task
        if outer:
            await state.lock.acquire()
            state.owner = task
        try:
            key = (ownership, id(config), id(settings))
            if key not in state.decisions:
                state.decisions[key] = gate.mode_for(ownership, config, settings)
            state.modes.append(state.decisions[key])
            try:
                yield
            finally:
                state.modes.pop()
        finally:
            if outer:
                state.decisions.clear()
                state.owner = None
                state.lock.release()


class ExecutionContext:
    """A gate and the coordinators shared by every construction path."""

    def __init__(self) -> None:
        self.authority: AuthorityStore | None = None
        self.planned_epoch: ContextVar[int | None] = ContextVar("planned_epoch", default=None)
        self.gate = WriteGate(self)
        self._buses: dict[tuple[str, str, str], BusCoordinator] = {}

    @contextmanager
    def planning(self) -> Iterator[None]:
        """Capture before computing work; never fill in a missing epoch at action time."""
        state = self.authority.state if self.authority is not None else None
        token = self.planned_epoch.set(state.epoch if state is not None else None)
        try:
            yield
        finally:
            self.planned_epoch.reset(token)

    def coordinator_for(self, config: DeviceConfig, settings: Settings) -> BusCoordinator:
        # All sensors/selects and slaves on a Solis Modbus hub share a bus.
        bus = settings.solis_control_hub if config.driver == "solis" else (
            settings.alphaess_serial or config.id
        )
        key = (settings.ha_rest_url, config.driver, bus)
        if key not in self._buses:
            self._buses[key] = BusCoordinator()
        return self._buses[key]


_DEFAULT_EXECUTION = ExecutionContext()


def execution_context() -> ExecutionContext:
    return _DEFAULT_EXECUTION


class _ActionDevice(Protocol):
    _config: DeviceConfig
    _settings: Settings
    _execution: ExecutionContext
    _coordinator: BusCoordinator


P = ParamSpec("P")
R = TypeVar("R")
D = TypeVar("D", bound=_ActionDevice)


def device_action(
    ownership: Ownership,
) -> Callable[
    [Callable[Concatenate[D, P], Coroutine[Any, Any, R]]],
    Callable[Concatenate[D, P], Coroutine[Any, Any, R]],
]:
    """Declare ownership and enclose prerequisites, writes and verification."""
    def decorate(
        method: Callable[Concatenate[D, P], Coroutine[Any, Any, R]],
    ) -> Callable[Concatenate[D, P], Coroutine[Any, Any, R]]:
        @wraps(method)
        async def run(self: D, /, *args: P.args, **kwargs: P.kwargs) -> R:
            async with self._coordinator.action(
                self._execution.gate, ownership, self._config, self._settings
            ):
                return await method(self, *args, **kwargs)

        run.ownership = ownership  # type: ignore[attr-defined]
        return run

    return decorate
