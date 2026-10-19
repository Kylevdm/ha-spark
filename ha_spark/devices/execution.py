"""Process-shared permission seam and indivisible, reentrant inverter bus actions.

The gate deliberately delegates to the legacy predicate: ownership is descriptive,
not new authority. Acquire before prerequisite reads and hold through read-back.
Factories and direct driver construction use the same default context. Coordinators
keep loop-local locks (CLI/tests may run successive event loops); the daemon uses
one event loop, and no lock is held across loops or shared with another process.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, Protocol, TypeVar
from weakref import WeakKeyDictionary

from ha_spark.devices.base import effective_mode

if TYPE_CHECKING:
    from ha_spark.config import DeviceConfig, Settings


class Ownership(StrEnum):
    PROTECTION = "protection"
    CHARGING = "charging"
    CLOCK = "clock"


class WriteGate:
    """The only permission implementation; no operating-mode authority yet."""

    def mode_for(self, ownership: Ownership, config: DeviceConfig, settings: Settings) -> str:
        return effective_mode(config.control, settings.proactive_mode)


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
        self.gate = WriteGate()
        self._buses: dict[tuple[str, str, str], BusCoordinator] = {}

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
