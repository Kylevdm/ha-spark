"""Process-shared permission seam and indivisible, reentrant inverter bus actions.

The daemon binds the sole authority store. Unbound callers fail closed. Planned
epochs, config generations and accepted limits revisions are checked under the
bus lock and again at each actuating call. Remapped held identities never grant
authority to a replacement.
During a transition only the running handover step actuates, for its own class.
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

from ha_spark.authority import HoldDevice, retirement_status

if TYPE_CHECKING:
    from datetime import datetime

    from ha_spark.authority import AuthorityStore
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.energy.topology import MajorLoadObservation
    from ha_spark.energy.topology_record import LoadedTopology


def held_identity_remapped(device: HoldDevice, settings: Settings) -> bool:
    """An existing hold must never authorise a replacement control or driver.

    A held identity that matches no configured device (its entity, hub, slave or
    driver changed, or the device was removed) blocks every device until Observe.
    """
    return all(device != HoldDevice(
        driver=config.driver, power_switch=config.entities.get("power_switch", ""),
        hub=settings.solis_control_hub, slave=settings.solis_modbus_slave,
    ) for config in settings.devices)


class Ownership(StrEnum):
    PROTECTION = "protection"
    CHARGING = "charging"
    CLOCK = "clock"


# During a transition only the running (first stored) step may actuate, and only its
# own ownership class. Steps not listed here (``commit_observe``) never write.
HANDOVER_STEP_OWNERSHIP: dict[str, frozenset[Ownership]] = {
    "evaluate_protection": frozenset({Ownership.PROTECTION}),
    "release_holds": frozenset({Ownership.PROTECTION}),
    "cancel_export": frozenset({Ownership.CHARGING}),
    "write_fallback": frozenset({Ownership.CHARGING}),
    # → Optimise stays activating until its first fresh plan verifies; that plan
    # and the hold reconcile keep running meanwhile, so protection never lapses.
    "verify_fresh_plan": frozenset({Ownership.CHARGING, Ownership.PROTECTION}),
}


class WriteGate:
    """The only permission implementation; legacy keys grant nothing."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def mode_for(self, ownership: Ownership, config: DeviceConfig, settings: Settings) -> str:
        if retirement_status(settings) is not None:
            return "refuse:authority unknown"
        store = self.context.authority
        if store is None or store.save_failed or not store.known:
            return "refuse:authority unknown"
        state = store.state
        assert state is not None
        if state.transition is not None:
            step = self.context.handover_step.get()
            steps = state.transition.steps
            if not steps or step != steps[0] or ownership not in (
                HANDOVER_STEP_OWNERSHIP.get(step, frozenset())
            ):
                return "refuse:not owned"
        if self.context.planned_epoch.get() != state.epoch:
            return "refuse:stale authority"
        if self.context.planned_generation.get() != self.context.config_generation:
            return "refuse:stale config"
        if self.context.planned_limits.get() != self.context.limits_key():
            return "refuse:stale limits"
        if state.committed_mode == "observe":
            return "simulate"
        if any(held_identity_remapped(h.device, settings) for h in state.holds):
            return "refuse:held identity remapped"
        if state.committed_mode == "protect" and ownership == Ownership.CHARGING:
            return "simulate"
        return "on"

    def recheck(self, ownership: Ownership, config: DeviceConfig, settings: Settings) -> str:
        """Write-time re-evaluation under the held lock: the same rules, not a new decision."""
        return self.mode_for(ownership, config, settings)


class AuthorityRevoked(RuntimeError):
    """Authority no longer permits the write this action was granted; nothing was sent."""


@dataclass
class ReleaseBackoff:
    """Consecutive refused hold releases on one device (#448), and the last refusal.

    In memory only: a restart starts from 0, which costs at most five more attempts.
    """

    refusals: int = 0
    last_at: datetime | None = None
    line: str = ""


@dataclass
class _BusState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task[Any] | None = None
    modes: list[str] = field(default_factory=list)
    checks: list[Callable[[], str]] = field(default_factory=list)
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

    def confirm_write(self) -> None:
        """Re-ask the gate immediately before an actuating call; raise unless still "on".

        The cached decision covers presentation and prerequisite reads only. Authority
        can change while they await (store deleted, save failed, epoch moved), so every
        hardware write re-checks under the held lock and is never sent on a stale grant.
        """
        state = self._state()
        if state.owner is not asyncio.current_task() or not state.checks:
            raise RuntimeError("Device write requested outside a held action")
        mode = state.checks[-1]()
        if mode != "on" or state.modes[-1] != "on":
            raise AuthorityRevoked(mode.removeprefix("refuse:"))

    @asynccontextmanager
    async def transition(self) -> AsyncIterator[None]:
        """Wait for a complete action, then own the bus across handover steps."""
        state = self._state()
        async with state.lock:
            state.owner = asyncio.current_task()
            try:
                yield
            finally:
                state.decisions.clear()
                state.owner = None

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
        # A transition owns the bus across steps; each top-level action still
        # takes a fresh decision, so one step's grant never carries to the next.
        top = not state.modes
        try:
            key = (ownership, id(config), id(settings))
            if key not in state.decisions:
                state.decisions[key] = gate.mode_for(ownership, config, settings)
            state.modes.append(state.decisions[key])
            state.checks.append(lambda: gate.recheck(ownership, config, settings))
            try:
                yield
            finally:
                state.checks.pop()
                state.modes.pop()
        finally:
            if top:
                state.decisions.clear()
            if outer:
                state.owner = None
                state.lock.release()


class ExecutionContext:
    """A gate and the coordinators shared by every construction path."""

    def __init__(self) -> None:
        self.authority: AuthorityStore | None = None
        self.planned_epoch: ContextVar[int | None] = ContextVar("planned_epoch", default=None)
        self.config_generation = 0
        self.planned_generation: ContextVar[int | None] = ContextVar(
            "planned_generation", default=None
        )
        self.handover_step: ContextVar[str | None] = ContextVar("handover_step", default=None)
        # The accepted limits record (#542): the daemon sets it at startup and on
        # every accepted snapshot change. None is the legacy settings. A write is
        # refused when the limits it was planned under have been superseded.
        self.limits_record: LoadedTopology | None = None
        # The accepted snapshot's major-load observations, for planning only.
        self.major_loads: tuple[MajorLoadObservation, ...] = ()
        self.planned_limits: ContextVar[tuple[str, int | None]] = ContextVar(
            "planned_limits", default=("legacy", None)
        )
        self.gate = WriteGate(self)
        self.release_backoff: dict[HoldDevice, ReleaseBackoff] = {}
        self._buses: dict[tuple[str, str, str], BusCoordinator] = {}

    @contextmanager
    def planning(self) -> Iterator[None]:
        """Capture before computing work; never fill missing fences at action time."""
        state = self.authority.state if self.authority is not None else None
        token = self.planned_epoch.set(state.epoch if state is not None else None)
        generation_token = self.planned_generation.set(self.config_generation)
        limits_token = self.planned_limits.set(self.limits_key())
        try:
            yield
        finally:
            self.planned_epoch.reset(token)
            self.planned_generation.reset(generation_token)
            self.planned_limits.reset(limits_token)

    def limits_key(self) -> tuple[str, int | None]:
        """The current accepted limits identity (kind, revision)."""
        record = self.limits_record
        return ("legacy", None) if record is None else (record.kind, record.revision)

    @contextmanager
    def after_authority_save(self) -> Iterator[None]:
        """Re-capture only the epoch after this daemon's own save.

        The config generation stays the one captured with the caller's settings
        snapshot, so a save that lands mid-tick still refuses work planned on it.
        """
        state = self.authority.state if self.authority is not None else None
        token = self.planned_epoch.set(state.epoch if state is not None else None)
        try:
            yield
        finally:
            self.planned_epoch.reset(token)

    @contextmanager
    def handover(self, step: str) -> Iterator[None]:
        """Run one step of the stored transition; the gate still checks every fence."""
        token = self.handover_step.set(step)
        try:
            yield
        finally:
            self.handover_step.reset(token)

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
