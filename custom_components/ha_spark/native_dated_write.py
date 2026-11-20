"""Daytime dated charge windows in the charge half of Solis Slot 2 (#700).

Slot 1 carries the overnight window and export; this intent touches only Slot 2,
whose block is fresh-read and written whole so its discharge half is kept.
Solis holds one charge current (43141) for every slot: the plan's charge
intents set it to the window that fires next, so arming writes only the clock
face. The durable record is saved before the write and marked ``armed`` only
after exact read-back. Clearing is #701.
"""
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util

from .authority_store import DatedChargeRecord
from .commands import (
    DatedChargeNone,
    DatedChargeUntrusted,
    DatedChargeWindow,
    IntentResult,
    WriteReason,
)
from .native_export import clock_face, export_not_yet_armed
from .native_export_write import SLOT2_REG, NativeExportWrites
from .topology import Inverter

if TYPE_CHECKING:
    from .native_slots import NativeSlots


class NativeDatedChargeWrites(NativeExportWrites):
    """Slot 2 transactions sharing the boundary's authority checks and locks."""

    if TYPE_CHECKING:
        verified_slot2: dict[str, tuple[int, tuple[int, ...]]]

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

    def _hold_active(self, inverter: Inverter) -> bool:
        state = self.coordinator.authority.state
        return state is not None and any(
            hold.device.power_switch == inverter.id for hold in state.holds)

    def _dated_write_reason(self, inverter: Inverter, epoch: int) -> WriteReason | None:
        """Write-time permission for a Slot 2 charge half: Optimise, no hold."""
        reason = self._export_policy_reason(inverter, epoch)
        if reason is None and self._hold_active(inverter):
            return "hold_active"
        return reason

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
                    # Until #701 retires records, "none" and "untrusted" keep the
                    # Slot 1 window's old rule: Slot 2 reads zero.
                    return await slots.block(SLOT2_REG, [0] * 8)

    async def _async_arm_dated(self, inverter: Inverter, request: DatedChargeWindow,
                               slots: "NativeSlots", epoch: int) -> IntentResult:
        c = self.coordinator
        now = dt_util.utcnow()
        zone = c.control.household_zone()
        soc = getattr(c, "soc", None)
        reason = self._dated_write_reason(inverter, epoch)
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
        if not await self._power_on(inverter, now):
            return IntentResult(outcome="refused", reason="power_switch_not_on")
        permission = await self._read(c.native.transport(inverter), inverter, input_=True)
        if permission is None or not permission & 32:
            return IntentResult(outcome="refused", reason="grid_charge_not_permitted")
        resident = await slots.read(SLOT2_REG)
        state = c.authority.state
        if resident is None or state is None:
            return IntentResult(outcome="failed")
        target = clock_face(request.start, request.end, zone) + resident[4:]
        record = self._dated_record(inverter.id)
        same = (record is not None and record.start == request.start
                and record.end == request.end)
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
        slots.charge_check = lambda: self._dated_write_reason(inverter, epoch)
        result = await slots.block(SLOT2_REG, target)
        if result.outcome not in ("applied", "unchanged"):
            return result
        if record.state != "armed" and not await self._save_dated(
                inverter.id, record.model_copy(update={
                    "state": "armed", "last_reconcile_at": now}), now):
            return IntentResult(outcome="unverified")
        self.verified_slot2[inverter.id] = (epoch, tuple(target))
        return result
