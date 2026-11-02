"""Native fallback causes, recovery, verified applies and transition steps.

Causes are recorded only in committed Optimise after the startup grace. The
schedule is revalidated against live limits before every apply, writes through
the one charge-write boundary and is counted only after exact read-back.
Recovery progress is in memory; the durable cause is cleared here.
"""
import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification

from .authority_store import AuthorityState, FallbackState
from .channel import Outcome
from .clock_sync import STALE_AFTER
from .commands import IntentResult
from .native_fallback import FallbackSchedule, validate_fallback
from .native_hold import REPEAT_AFTER
from .topology import Inverter, Topology, effective_limits, validate_topology

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator

CAUSES = ("addon_liveness", "soc_integrity")
_LIVENESS_RECOVERY = timedelta(minutes=5)
_RETRY = timedelta(minutes=5)
_MAX_ATTEMPTS = 3


class NativeFallback:
    """One schedule, two independent causes and memory-only recovery streaks."""

    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.lock = asyncio.Lock()
        self.verified: dict[str, tuple[int, tuple[int, ...]]] = {}
        self.attempts: dict[str, int] = {}
        self.backoff: dict[str, datetime] = {}
        self.retry_at: datetime | None = None
        self.reapply_at: datetime | None = None
        self.exit_since: datetime | None = None
        self.liveness_since: datetime | None = None
        self.notices: dict[str, tuple[datetime, bool]] = {}

    # --- evidence and configuration -----------------------------------------

    def observe_outcome(self, outcome: Outcome, now: datetime) -> None:
        """Any non-counted outcome breaks the recovery streak."""
        if outcome is Outcome.COUNTED:
            if self.liveness_since is None:
                self.liveness_since = now
        else:
            self.liveness_since = None

    def retrying(self, now: datetime) -> bool:
        """After a failed exit plan, plans wait out the five-minute retry."""
        return self.retry_at is not None and now < self.retry_at

    def liveness_lost(self) -> None:
        self.liveness_since = None

    def _topology(self) -> Topology | None:
        topology = validate_topology(self.coordinator.entry.options.get("topology"))
        return topology if isinstance(topology, Topology) else None

    def _native(self) -> list[Inverter]:
        topology = self._topology()
        return [] if topology is None else [
            i for i in topology.inverters if i.transport == "native"]

    def _schedule(self) -> FallbackSchedule | str:
        topology = self._topology()
        if topology is None:
            return "fallback_topology_invalid"
        return validate_fallback(
            self.coordinator.entry.options.get("fallback"), topology,
            effective_limits(topology),
        )

    def valid(self, now: datetime) -> bool:
        """Optimise needs a valid schedule and a fresh grid-charge permission read."""
        if isinstance(self._schedule(), str):
            return False
        native = self._native()
        if not native:
            return False
        for inverter in native:
            reading = self.coordinator.native.reading(inverter, "input_33132")
            if (reading is None or not reading.registers
                    or not timedelta(0) <= now - reading.read_at <= STALE_AFTER
                    or not reading.registers[0] & 32):
                return False
        return True

    def in_force(self) -> bool:
        state = self.coordinator.authority.state
        if state is None or not state.fallback.active:
            return False
        native = self._native()
        return bool(native) and all(self._verified(i.id, state.epoch) for i in native)

    def _verified(self, inverter_id: str, epoch: int) -> bool:
        verified = self.verified.get(inverter_id)
        return verified is not None and verified[0] == epoch

    # --- notifications -------------------------------------------------------

    def _identifier(self, kind: str) -> str:
        return f"ha_spark_fallback_{kind}"

    def _create(self, kind: str, message: str) -> None:
        persistent_notification.async_create(
            self.coordinator.hass, message, title="ha-spark fallback",
            notification_id=self._identifier(kind),
        )

    def _dismiss(self, kind: str) -> None:
        persistent_notification.async_dismiss(
            self.coordinator.hass, self._identifier(kind))

    def _entered(self, causes: tuple[str, ...]) -> None:
        self.notices.pop("unverified", None)
        self._dismiss("cleared")
        self._create("entered", "Fallback charging is active: " + ", ".join(causes) + ".")

    def _cleared(self) -> None:
        self.notices.pop("unverified", None)
        self._dismiss("entered")
        self._dismiss("unverified")
        self._create("cleared", "Fallback charging is no longer active.")

    def _unverified(self, reason: str, now: datetime) -> None:
        previous = self.notices.get("unverified")
        if previous is not None and (previous[1] or now - previous[0] < REPEAT_AFTER):
            return
        self.notices["unverified"] = (previous[0] if previous else now, previous is not None)
        self._create("unverified",
                     f"Fallback charging is not verified: {reason}.")

    def _in_force_notice(self) -> None:
        if self.notices.pop("unverified", None) is not None:
            self._dismiss("unverified")

    # --- evaluation ----------------------------------------------------------

    async def async_evaluate(self, now: datetime) -> None:
        """Called by the thirty-second native tick, after holds reconcile."""
        async with self.coordinator.control.lock:
            async with self.lock:
                await self._evaluate(now)

    async def _evaluate(self, now: datetime) -> None:
        c = self.coordinator
        state = c.authority.state
        if (state is None or not c.control.owned() or not c.control.startup_complete
                or not c.control.permits_non_protection(now)
                or state.committed_mode != "optimise"
                or state.manual_session is not None
                or state.transition is not None and state.transition.target != "optimise"):
            return
        active = state.fallback.active
        causes = set(state.fallback.causes)
        if "addon_liveness" in causes:
            if (c.connected and self.liveness_since is not None
                    and now - self.liveness_since >= _LIVENESS_RECOVERY):
                causes.discard("addon_liveness")
        elif not c.connected:
            causes.add("addon_liveness")
        if "soc_integrity" in causes:
            if c.soc.recovery_ready:
                causes.discard("soc_integrity")
        elif c.soc.fallback_requested:
            causes.add("soc_integrity")
        desired = tuple(cause for cause in CAUSES if cause in causes)
        if active and desired and not state.fallback.causes:
            # An exit plan may have overwritten the schedule: re-verify it.
            self.verified.clear()
        if desired != state.fallback.causes or desired and not active:
            if active:
                fallback = state.fallback.model_copy(update={"causes": desired})
            else:
                fallback = FallbackState(active=True, causes=desired, entered_at=now)
            if not await c.control.async_fallback_held(fallback, now=now):
                return
            state = c.authority.state
            if state is None:
                return
            if not active and state.fallback.active:
                self._entered(state.fallback.causes)
                # Fallback entry is a refused export path: clear it first.
                await self._cancel_export(state.epoch)
        if not state.fallback.active:
            return
        if state.fallback.causes:
            self.exit_since = None
            self.retry_at = None
            self.reapply_at = None
            await self._apply_causes(state, now)
            c.async_update_listeners()
            return
        if self.exit_since is None:
            self.exit_since = now
        await self._exit_pending(state, now)
        c.async_update_listeners()

    async def _apply_causes(self, state: AuthorityState, now: datetime) -> None:
        schedule = self._schedule()
        if isinstance(schedule, str):
            self._unverified(schedule, now)
            return
        native = self._native()
        pending = [i for i in native if not self._verified(i.id, state.epoch)]
        if not pending:
            self._in_force_notice()
            return
        failures: list[str] = []
        attempted = False
        for inverter in pending:
            backoff = self.backoff.get(inverter.id)
            if backoff is not None and now - backoff < _RETRY:
                continue
            result = await self._apply_one(inverter, schedule, state.epoch)
            attempted = True
            if result.outcome in ("applied", "unchanged"):
                self.attempts.pop(inverter.id, None)
                self.backoff.pop(inverter.id, None)
            else:
                failures.append(result.reason or "unverified")
                count = self.attempts.get(inverter.id, 0) + 1
                self.attempts[inverter.id] = count
                if count >= _MAX_ATTEMPTS:
                    self.backoff[inverter.id] = now
        if all(self._verified(i.id, state.epoch) for i in native):
            self._in_force_notice()
        elif attempted:
            self._unverified(failures[0] if failures else "unverified", now)

    async def _exit_pending(self, state: AuthorityState, now: datetime) -> None:
        if self.retrying(now):
            return
        # Only plans accepted after the exit began, or after the last retry, count.
        since = self.exit_since or now
        if self.retry_at is not None:
            since = max(since, self.retry_at)
        last = self.coordinator.writes.last_apply
        failed = (last is not None and last[0] == state.epoch and last[1] >= since
                  and not last[2])
        if failed:
            self.verified.clear()
            await self._apply_all(state.epoch)
            self.retry_at = now + _RETRY
            return
        if self.coordinator.writes.plan_verified(state.epoch, since=since):
            if await self.coordinator.control.async_fallback_held(FallbackState(), now=now):
                self.exit_since = None
                self.retry_at = None
                self.reapply_at = None
                self.verified.clear()
                self.attempts.clear()
                self.backoff.clear()
                self._cleared()
            return
        native = self._native()
        if all(self._verified(i.id, state.epoch) for i in native):
            return
        if self.reapply_at is None or now >= self.reapply_at:
            await self._apply_all(state.epoch)
            self.reapply_at = now + _RETRY

    async def _apply_all(self, epoch: int) -> None:
        schedule = self._schedule()
        if isinstance(schedule, str):
            return
        for inverter in self._native():
            await self._apply_one(inverter, schedule, epoch)

    async def _apply_one(self, inverter: Inverter, schedule: FallbackSchedule,
                         epoch: int) -> IntentResult:
        result = await self.coordinator.writes.async_fallback(
            inverter.id, current_a=schedule.currents[inverter.id],
            start=schedule.window_start.strftime("%H:%M"),
            end=schedule.window_end.strftime("%H:%M"), epoch=epoch,
        )
        if result.outcome in ("applied", "unchanged"):
            verified = self.coordinator.writes.verified_fallback.get(inverter.id)
            if verified is not None:
                self.verified[inverter.id] = verified
        return result

    # --- transition steps ----------------------------------------------------

    async def async_step(self, step: str, state: AuthorityState, *, now: datetime) -> bool:
        """Executed by the transition writer while it holds the authority lock."""
        async with self.lock:
            if step == "verify_fresh_plan":
                started = state.transition.started_at if state.transition is not None else now
                return self.coordinator.writes.plan_verified(state.epoch, since=started)
            if step == "cancel_export":
                return await self._cancel_export(state.epoch)
            if step == "write_fallback":
                return await self._write_fallback(state, now)
            return False

    async def _cancel_export(self, epoch: int) -> bool:
        results = [await self.coordinator.writes.async_cancel_export(i.id, epoch=epoch)
                   for i in self._native()]
        return all(result.outcome in ("applied", "unchanged") for result in results)

    async def _write_fallback(self, state: AuthorityState, now: datetime) -> bool:
        schedule = self._schedule()
        target = state.transition.target if state.transition is not None else None
        if isinstance(schedule, str):
            # Observe is the owner's safe exit; a refused restore must not trap it.
            return target == "observe"
        results = [await self._apply_one(i, schedule, state.epoch) for i in self._native()]
        if all(result.outcome in ("applied", "unchanged") for result in results):
            return True
        return target == "observe" and all(
            result.outcome in ("applied", "unchanged", "refused") for result in results)

