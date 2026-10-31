"""Verified native transition execution, with fail-closed device/evidence seams.

No native writer is installed yet. Later device tickets supply the step executor
and startup reads. A step returns True only after its device read-back verifies;
verify_fresh_plan must consume a fresh plan from the current epoch, never a cache.
The writer must use the shared charge-write boundary and recheck ownership there.
"""
import asyncio
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from . import authority
from .authority_store import AuthorityState, FallbackState
from .transitions import Preconditions, TransitionDecision, TransitionRequest, propose_transition


class TransitionWriter(Protocol):
    async def async_barrier(self) -> None:
        """Wait for in-flight writes on every bus without cancelling them."""
        ...

    async def async_step(self, step: str, state: AuthorityState, *, removal: bool) -> bool:
        """Execute and verify; removal releases only on fresh clear evidence."""
        ...

    async def async_startup(self, stage: str) -> bool:
        """Connect, read device state/evidence, then reconcile holds."""
        ...


class AuthorityControl:
    def __init__(self, entry: ConfigEntry[Any], store: authority.AuthorityStore) -> None:
        self.entry = entry
        self.store = store
        self.writer: TransitionWriter | None = None
        self.preconditions: Callable[[], Preconditions] = Preconditions
        self.lock = asyncio.Lock()
        self.grace_until: datetime | None = None
        self.ready = False
        self.startup_complete = False

    def household_zone(self) -> ZoneInfo | None:
        raw = self.entry.options.get("household_time_zone", "Europe/London")
        if not isinstance(raw, str):
            return None
        try:
            return ZoneInfo(raw)
        except (ZoneInfoNotFoundError, ValueError):
            return None

    def owned(self) -> bool:
        return authority.authority_owner(self.entry) == "integration"

    async def async_request(
        self, request: TransitionRequest, *, now: datetime, zone: ZoneInfo,
        at: time = time(7), removal: bool = False,
    ) -> TransitionDecision:
        async with self.lock:
            if not self.owned():
                return TransitionDecision(refusal="not_owner")
            decision = propose_transition(self.store.state, request, self.preconditions(),
                                          now=now, zone=zone, at=at)
            if decision.state is None or decision.state == self.store.state:
                return decision
            before = self.store.state
            if self.writer is not None:
                try:
                    await self.writer.async_barrier()
                except Exception:
                    return TransitionDecision(refusal="transition_in_progress")
            if not self.owned() or self.store.state != before:
                return TransitionDecision(refusal="authority_unknown")
            if not await self.store.async_save(decision.state):
                return TransitionDecision(refusal="authority_unknown")
            if not (before is not None and before.manual_session is not None
                    and request.target in ("protect", "optimise")):
                await self._advance(removal=removal)
            return TransitionDecision(state=self.store.state)

    async def _advance(self, *, removal: bool = False) -> None:
        state = self.store.state
        if not self.owned() or state is None or state.transition is None:
            return
        if state.committed_mode == "observe" or self.writer is None or not self.ready:
            return
        while state.transition is not None and state.transition.steps:
            if not self.owned() or self.store.state != state:
                return
            step = state.transition.steps[0]
            if (step in ("verify_fresh_plan", "write_fallback", "cancel_export")
                    and not self.permits_non_protection(dt_util.utcnow())):
                return
            try:
                verified = await self.writer.async_step(step, state, removal=removal)
            except Exception:
                verified = False
            if not self.owned() or self.store.state != state:
                return
            transition = state.transition.model_copy(update={
                "steps": state.transition.steps[1:] if verified else state.transition.steps,
                "failing_actions": () if verified else (step,),
            })
            candidate = state.model_copy(update={"transition": transition})
            if not await self.store.async_save(candidate) or not verified:
                return
            state = candidate
        if state.transition is not None:
            # Only now claim the verified handover. No epoch bump for step progress.
            ends_fallback = (state.fallback.active and state.manual_session is None
                             and state.transition.target != "optimise")
            await self.store.async_save(state.model_copy(update={
                "committed_mode": state.transition.target, "transition": None,
                "fallback": FallbackState() if ends_fallback else state.fallback,
                "epoch": state.epoch + int(ends_fallback),
            }))

    async def async_retry(self) -> None:
        async with self.lock:
            await self._advance()

    async def async_expire(self, *, now: datetime, zone: ZoneInfo) -> None:
        if not self.startup_complete:
            return
        state = self.store.state
        manual = state.manual_session if state is not None else None
        if manual is not None and manual.deadline is not None and now >= manual.deadline:
            await self.async_request(TransitionRequest(target="resume"), now=now, zone=zone)

    async def async_start(self, *, now: datetime, zone: ZoneInfo) -> None:
        """Load precedes this call; refresh UTC after reads, never age grace during I/O."""
        self.ready = False
        self.startup_complete = False
        self.grace_until = None
        if not self.owned() or self.store.state is None or self.writer is None:
            return
        for stage in ("connect", "read_device_state", "read_evidence", "reconcile_holds"):
            state = self.store.state
            if not self.owned() or state is None:
                return
            if stage == "reconcile_holds" and state.committed_mode == "observe":
                continue
            try:
                if not await self.writer.async_startup(stage):
                    return
            except Exception:
                return
        self.startup_complete = True
        # Expiry may persist a new grant, but ready stays false so it cannot run
        # non-protection steps before the full post-reconciliation grace.
        await self.async_expire(now=dt_util.utcnow(), zone=zone)
        self.grace_until = dt_util.utcnow() + timedelta(minutes=2)
        self.ready = True

    def permits_non_protection(self, now: datetime) -> bool:
        return (self.owned() and self.store.state is not None and self.ready
                and self.grace_until is not None and now >= self.grace_until)

    async def async_fallback(self, fallback: FallbackState, *, now: datetime) -> bool:
        """Called by the fallback evaluator; changes to causes alone do not bump epoch."""
        async with self.lock:
            state = self.store.state
            if not self.owned() or state is None:
                return False
            return await self.store.async_save(state.model_copy(update={
                "fallback": fallback,
                "epoch": state.epoch + int(fallback.active != state.fallback.active),
                "last_changed_at": now,
            }))

    async def async_new_session(self, session_id: str, *, now: datetime) -> bool:
        async with self.lock:
            state = self.store.state
            if not self.owned() or state is None:
                return False
            if state.addon_session_id == session_id:
                return True
            return await self.store.async_save(state.model_copy(update={
                "addon_session_id": session_id, "epoch": state.epoch + 1,
                "last_changed_at": now,
            }))
