"""Daytime dated charge windows in the charge half of Solis Slot 2 (#700).

Slot 1 carries the overnight window and export; this intent touches only Slot 2,
whose block is fresh-read and written whole so its discharge half is kept.
Solis holds one charge current (43141) for every slot: the plan's charge
intents set it to the window that fires next, so arming writes only the clock
face. The durable record is saved before the write and marked ``armed`` only
after exact read-back.

A clear (#701) zeroes only the charge half, keeps the discharge half, and is
confirmed by five further zero reads before the record is retired. A failed
clear keeps the record and raises a repair: the window would otherwise recur
daily at that clock time.

With the owner's opt-in (#769), a dispatch hold hands its switch On only after
the window is armed and read back (``native_handover``).
"""
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from . import native_handover, native_write
from .authority_store import DatedChargeRecord
from .commands import (
    DatedChargeNone,
    DatedChargeUntrusted,
    DatedChargeWindow,
    IntentResult,
    WriteReason,
)
from .const import DOMAIN
from .native_export import clock_face, export_not_yet_armed
from .native_export_write import SLOT2_REG, NativeExportWrites
from .native_power import async_write_power_locked
from .native_verify import confirm
from .topology import Inverter

if TYPE_CHECKING:
    from .native_slots import NativeSlots


class NativeDatedChargeWrites(NativeExportWrites):
    """Slot 2 transactions sharing the boundary's authority checks and locks."""

    if TYPE_CHECKING:
        verified_slot2: dict[str, tuple[int, tuple[int, ...]]]
        handovers: dict[str, native_handover.Handover]
        reclaimed: dict[str, set[datetime]]
        off_owed: set[str]

    def _dated_record(self, inverter_id: str) -> DatedChargeRecord | None:
        state = self.coordinator.authority.state
        if state is None:
            return None
        return next((row for row in state.dated_charges if row.inverter_id == inverter_id),
                    None)

    async def _save_dated(self, inverter_id: str, record: DatedChargeRecord,
                          now: datetime) -> bool:
        state = self.coordinator.authority.state
        if state is None:
            return False
        rows = tuple(row for row in state.dated_charges if row.inverter_id != inverter_id)
        return await self.coordinator.authority.async_save(state.model_copy(update={
            "dated_charges": (*rows, record), "last_changed_at": now}))

    async def _retire_dated(self, inverter_id: str, now: datetime) -> bool:
        state = self.coordinator.authority.state
        if state is None:
            return False
        rows = tuple(row for row in state.dated_charges if row.inverter_id != inverter_id)
        if len(rows) == len(state.dated_charges):
            return True
        return await self.coordinator.authority.async_save(state.model_copy(update={
            "dated_charges": rows, "last_changed_at": now}))

    def _dated_issue_id(self, inverter_id: str) -> str:
        key = uuid5(NAMESPACE_URL, f"{self.coordinator.entry.entry_id}:{inverter_id}")
        return f"dated_clear_failed_{key}"

    async def _dated_clear_failed(self, inverter_id: str, now: datetime) -> None:
        """Keep the obligation durable for the tick's retry and tell the owner."""
        record = self._dated_record(inverter_id)
        if record is not None and record.state != "clear_pending":
            await self._save_dated(inverter_id, record.model_copy(update={
                "state": "clear_pending", "last_reconcile_at": now}), now)
        ir.async_create_issue(
            self.coordinator.hass, DOMAIN, self._dated_issue_id(inverter_id),
            is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="dated_clear_failed",
            data={"entry_id": self.coordinator.entry.entry_id, "inverter_id": inverter_id})

    async def _async_clear_dated_locked(self, inverter: Inverter, slots: "NativeSlots",
                                        now: datetime) -> IntentResult:
        """Zero the charge half, confirm it on five further reads, retire the record."""
        record = self._dated_record(inverter.id)
        resident = await slots.read(SLOT2_REG)
        result = (IntentResult(outcome="failed") if resident is None
                  else await slots.block(SLOT2_REG, [0, 0, 0, 0] + resident[4:]))
        if result.outcome not in ("applied", "unchanged"):
            if record is not None or result.outcome != "refused":
                await self._dated_clear_failed(inverter.id, now)
            return result
        if record is None and result.outcome == "unchanged":
            return result

        async def half() -> list[int] | None:
            block = await slots.read(SLOT2_REG)
            return None if block is None else block[:4]
        if (not await confirm([0] * 4, half, native_write.verify_sleep, changed=True,
                              mismatch=slots.mismatch)
                or not await self._retire_dated(inverter.id, now)):
            await self._dated_clear_failed(inverter.id, now)
            return IntentResult(outcome="unverified")
        self.verified_slot2.pop(inverter.id, None)
        ir.async_delete_issue(self.coordinator.hass, DOMAIN, self._dated_issue_id(inverter.id))
        return result

    def _hold_active(self, inverter: Inverter) -> bool:
        state = self.coordinator.authority.state
        return state is not None and any(
            hold.device.power_switch == inverter.id for hold in state.holds)

    def _dated_write_reason(self, inverter: Inverter, epoch: int, *,
                            handover: bool = False) -> WriteReason | None:
        """Write-time permission for a Slot 2 charge half: Optimise, no hold.

        ``handover``: the hold may hand its switch to this window (#769).
        """
        reason = self._export_policy_reason(inverter, epoch)
        if reason is None and self._hold_active(inverter) and not handover:
            return "hold_active"
        return reason

    def _may_hand_over(self, inverter: Inverter, request: DatedChargeWindow,
                       now: datetime) -> bool:
        """A held inverter's window that the hold may hand its switch to."""
        c = self.coordinator
        return (request.target_soc_pct is not None and self._hold_active(inverter)
                and native_handover.minute(request.end)
                not in self.reclaimed.get(inverter.id, set())
                and native_handover.conditions(c, inverter, request.start, request.end,
                                               request.target_soc_pct, now,
                                               c.holds.evidence.read(now)))

    async def _async_hand_over(self, inverter: Inverter, request: DatedChargeWindow,
                               slots: "NativeSlots", epoch: int,
                               result: IntentResult) -> IntentResult:
        """Record the handover, then turn the switch On under a final fresh recheck."""
        c = self.coordinator
        state = c.authority.state
        assert state is not None and request.target_soc_pct is not None
        handover = native_handover.Handover(
            request.start, request.end, request.target_soc_pct, state.epoch,
            c.native.transport(inverter).connection_epoch, dt_util.utcnow())
        # Before the write: any outcome but a clean refusal leaves the hold an Off to verify.
        self.handovers[inverter.id] = handover

        def recheck() -> str | None:
            now = dt_util.utcnow()
            return (self._dated_write_reason(inverter, epoch, handover=True)
                    or c.holds._refusal(inverter, state, release=False)
                    or (None if native_handover.live(c, inverter, handover, now,
                                                     c.holds.evidence.read(now))
                        else "hold_active"))

        power = await async_write_power_locked(c.native, inverter, "on", recheck=recheck)
        if power.mismatch:
            slots.mismatch()
        if power.outcome == "applied":
            return result.model_copy(update={"outcome": "applied"})
        if power.outcome in ("refused", "unreadable"):
            self.handovers.pop(inverter.id, None)
        return IntentResult(outcome="refused", reason="hold_active")

    async def async_dated(
        self, request: DatedChargeNone | DatedChargeUntrusted | DatedChargeWindow,
        *, epoch: int, mismatches: set[str],
    ) -> IntentResult:
        """One Slot 2 transaction; record saves serialise with authority saves."""
        c = self.coordinator
        guard = c.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        inverter = next((i for i in guard.topology.inverters
                         if i.id == request.inverter_id), None)
        if inverter is None:
            return IntentResult(outcome="refused", reason="limits")
        from .native_slots import NativeSlots

        async with c.control.lock:
            async with guard.lock:
                async with c.native.lock:
                    slots = NativeSlots(self, inverter, epoch, mismatches)
                    if isinstance(request, DatedChargeWindow):
                        return await self._async_arm_dated(inverter, request, slots, epoch)
                    now = dt_util.utcnow()
                    if (isinstance(request, DatedChargeUntrusted)
                            and await self._dated_kept(inverter, slots, now)):
                        return IntentResult(outcome="unchanged")
                    return await self._async_clear_dated_locked(inverter, slots, now)

    async def async_retry_dated(self, now: datetime) -> None:
        """Native tick: clear each record that is no longer live; Observe writes nothing.

        Needs no contact from the add-on, so a restart or Re-probe still clears
        a window at its end from the durable record alone.
        """
        c = self.coordinator
        async with c.control.lock:
            state = c.authority.state
            if (state is None or state.committed_mode == "observe" or not c.control.owned()
                    or state.transition is not None
                    and "cancel_export" in state.transition.steps):
                return
            live = (state.committed_mode == "optimise" and state.manual_session is None
                    and not state.fallback.active
                    and (state.transition is None or state.transition.target == "optimise"))
            for record in state.dated_charges:
                if (live and record.state != "clear_pending" and record.epoch == state.epoch
                        and record.store_id == state.store_id
                        and record.end.astimezone(UTC) > now):
                    continue
                await self.async_clear_dated(record.inverter_id, epoch=state.epoch,
                                             cleanup=True)

    async def async_clear_dated(self, inverter_id: str, *, epoch: int,
                                transition: bool = False,
                                cleanup: bool = False) -> IntentResult:
        """A clear under a transition's or a cleanup's own write policy.

        The caller holds control.lock, so the record's retirement serialises
        with every other authority save.
        """
        c = self.coordinator
        if self._dated_record(inverter_id) is None:
            # Nothing armed: a transition's export cancel has already zeroed Slot 2.
            return IntentResult(outcome="unchanged")
        guard = c.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        inverter = next((i for i in guard.topology.inverters if i.id == inverter_id), None)
        if inverter is None:
            return IntentResult(outcome="refused", reason="limits")
        from .native_slots import NativeSlots

        mismatches: set[str] = set()
        try:
            async with guard.lock:
                async with c.native.lock:
                    slots = NativeSlots(self, inverter, epoch, mismatches,
                                        transition=transition, cleanup=cleanup)
                    return await self._async_clear_dated_locked(
                        inverter, slots, dt_util.utcnow())
        finally:
            for mismatched in mismatches:
                await c.native.async_readback_mismatch(mismatched)

    async def _dated_kept(self, inverter: Inverter, slots: "NativeSlots",
                          now: datetime) -> bool:
        """An armed record of this epoch, under way, whose clock face is resident.

        A window that has not started is cleared: untrusted evidence no longer
        justifies it, and the next trusted plan re-arms it if it still stands.
        """
        c = self.coordinator
        state = c.authority.state
        record = self._dated_record(inverter.id)
        zone = c.control.household_zone()
        if (state is None or record is None or zone is None or record.state != "armed"
                or record.epoch != state.epoch or record.store_id != state.store_id
                or not record.start.astimezone(UTC) <= now < record.end.astimezone(UTC)):
            return False
        resident = await slots.read(SLOT2_REG)
        if resident is None or resident[:4] != clock_face(record.start, record.end, zone):
            return False
        self.verified_slot2[inverter.id] = (state.epoch, tuple(resident))
        return True

    async def _async_arm_dated(self, inverter: Inverter, request: DatedChargeWindow,
                               slots: "NativeSlots", epoch: int) -> IntentResult:
        c = self.coordinator
        now = dt_util.utcnow()
        zone = c.control.household_zone()
        soc = getattr(c, "soc", None)
        current = self.handovers.get(inverter.id)
        if current is not None and current.end == request.end:
            # The same purchase under a new plan (a later start is its remainder)
            # stops at the new target; with none, at once.
            self.handovers[inverter.id] = replace(
                current, target_soc_pct=request.target_soc_pct or 0.0)
        handover = self._may_hand_over(inverter, request, now)
        reason = self._dated_write_reason(inverter, epoch, handover=handover)
        if reason is None and request.end.astimezone(UTC) <= now:
            reason = "invalid_intent"
        if reason is None and (soc is None or not soc.increases_allowed(inverter.bank_id)):
            reason = "untrusted_soc"
        if reason is None and (zone is None or self._clock_refused(inverter, now)):
            reason = "clock_not_trusted"
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        assert zone is not None
        # Slot 2 holds a time of day and no date: arm only for its next occurrence.
        if export_not_yet_armed(request.start.astimezone(zone), request.end.astimezone(zone),
                                now.astimezone(zone)):
            return IntentResult(outcome="deferred")
        power = await self._power_state(inverter)
        if inverter.id in self.handovers and power == "off":
            # An Off seen under a handover is its take-back, whoever wrote it.
            c.holds._taken_back(inverter)
            handover = handover and native_handover.minute(request.end) not in (
                self.reclaimed.get(inverter.id, set()))
            if not handover:
                return IntentResult(outcome="refused", reason="hold_active")
        handed = self.handovers.get(inverter.id)
        same_handover = handed is not None and (handed.start, handed.end) == (
            request.start, request.end)
        if not handover and power != "on":
            return IntentResult(outcome="refused", reason="power_switch_not_on")
        # A hold hands over from its own Off; only its current window may find it On.
        if handover and power != "off" and not (power == "on" and same_handover):
            return IntentResult(outcome="refused", reason="hold_active")
        permission = await self._read(c.native.transport(inverter), inverter, input_=True)
        if permission is None or not permission & 32:
            if permission is not None and inverter.id in self.handovers:
                self.off_owed.add(inverter.id)  # read directly: the window cannot charge
            return IntentResult(outcome="refused", reason="grid_charge_not_permitted")
        resident = await slots.read(SLOT2_REG)
        state = c.authority.state
        if resident is None or state is None:
            return IntentResult(outcome="failed")
        target = clock_face(request.start, request.end, zone) + resident[4:]
        record = self._dated_record(inverter.id)
        # A record from another epoch or store is replaced, never re-armed as is:
        # the tick would clear it again and the next apply would re-arm it.
        same = (record is not None and record.start == request.start
                and record.end == request.end and record.epoch == state.epoch
                and record.store_id == state.store_id)
        if not same:
            # Write-ahead: a failed save writes nothing at all.
            record = DatedChargeRecord(
                inverter_id=inverter.id, start=request.start, end=request.end,
                store_id=state.store_id, epoch=state.epoch, state="unknown",
                last_reconcile_at=now)
            if not await self._save_dated(inverter.id, record, now):
                return IntentResult(outcome="refused", reason="authority_unknown")
        assert record is not None
        slots.soc_required = True
        slots.charge_check = lambda: self._dated_write_reason(inverter, epoch, handover=handover)
        result = await slots.block(SLOT2_REG, target)
        if result.outcome not in ("applied", "unchanged"):
            return result
        if record.state != "armed" and not await self._save_dated(
                inverter.id, record.model_copy(update={
                    "state": "armed", "last_reconcile_at": now}), now):
            return IntentResult(outcome="unverified")
        self.verified_slot2[inverter.id] = (epoch, tuple(target))
        if handover and not (power == "on" and same_handover):
            return await self._async_hand_over(inverter, request, slots, epoch, result)
        return result
