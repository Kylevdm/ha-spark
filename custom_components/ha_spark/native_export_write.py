"""Native export arm, clear and reconcile inside the shared charge-write boundary."""
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

from homeassistant.util import dt as dt_util

from . import native_export_log, native_write
from .authority_store import EventIdentity, ExportRecord, ExportSupervision
from .authority_store import ExportWindow as StoredWindow
from .clock_sync import DRIFT_TOLERANCE, STALE_AFTER, clock_error
from .commands import (
    ChargeCurrentLimit,
    ChargeWindow,
    ExportNone,
    ExportUntrusted,
    ExportWindow,
    IntentResult,
    WriteReason,
)
from .native_export import (
    EXPORT_CURRENT_A,
    DeviceExportFact,
    bound_refusal,
    claim_from_raw,
    clock_face,
    export_not_yet_armed,
    latest_record,
    record_is_live,
    site_export_limit_w,
    supervision_for,
)
from .native_power import POWER_SWITCH_REG, decode_power
from .native_verify import confirm
from .solis_frames import Reading
from .solis_transport import Transport, valid_words
from .topology import Inverter, effective_limits

EXPORT_CURRENT_REG = 43142
SLOT1_REG = 43143
SLOT2_REG = 43153
SLOT3_REG = 43163
MODEL_REG = 33000
INVERTER_TYPE_REG = 35000
TOU_VERSION_REG = 33289
POWER_SWITCH_FRAME = f"holding_{POWER_SWITCH_REG}"

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .guard_loop import SupplyGuardLoop
    from .native_slots import NativeSlots


class NativeExportWrites:
    """Export transactions sharing the host boundary's authority checks and locks."""

    if TYPE_CHECKING:
        coordinator: SparkCoordinator
        written_a: dict[str, float]
        written_at: dict[str, datetime]
        verified_windows: dict[str, tuple[int, tuple[int, ...]]]
        verified_fallback: dict[str, tuple[int, tuple[int, ...]]]
        reverified: set[str]
        armed_this_session: set[str]

        def _refusal(self, inverter: Inverter, epoch: int, *,
                     guard_write: bool = False,
                     entity_control: str | None = None,
                     fallback: bool = False,
                     transition: bool = False,
                     cleanup: bool = False) -> WriteReason | None:
            ...

        async def _async_held(self, guard: "SupplyGuardLoop",
                              request: ChargeCurrentLimit | ChargeWindow | ExportNone,
                              epoch: int, mismatches: set[str], *,
                              slots: "NativeSlots | None" = None) -> IntentResult:
            ...

        @staticmethod
        def _restore(guard: "SupplyGuardLoop", inverter_id: str, previous: float | None) -> None:
            ...

        async def _async_transaction(self, guard: "SupplyGuardLoop", inverter: Inverter,
                                     request: ChargeCurrentLimit, epoch: int,
                                     mismatches: set[str], *,
                                     commit: (
                                         Callable[[Inverter, float], Awaitable[IntentResult]] | None
                                     ) = None) -> tuple[IntentResult, bool]:
            ...

        @staticmethod
        async def _read(transport: Transport, inverter: Inverter, *, input_: bool = False
                        ) -> int | None:
            ...

        async def _async_commit(self, inverter: Inverter, current: float, epoch: int,
                                mismatches: set[str], *, soc_required: bool = False,
                                soc_exempt: bool = False,
                                guard_write: bool = False, lower_only: bool = False,
                                fallback: bool = False, transition: bool = False,
                                entity_control: str | None = None) -> IntentResult:
            ...

    async def async_reconcile_export(self, now: datetime) -> bool:
        """Startup stage: re-verify every loaded export record against Slot 1.

        A record with the same epoch and store identity, its end ahead, no stop
        latch and a matching supervision is ``preserved`` when the resident
        discharge half exactly matches its clock face; anything else is cleared
        (or cancelled when the epoch or store changed). Nothing is ever
        re-armed from the record alone.
        """
        return await self._async_reconcile_all(now, strict=True)

    async def async_reconcile_pending(self, now: datetime) -> None:
        """Retry a re-verification the startup stage could not attempt yet."""
        await self._async_reconcile_all(now, strict=False)

    async def _async_reconcile_all(self, now: datetime, *, strict: bool) -> bool:
        """One ordered pass under control.lock then guard.lock then native.lock.

        Setup creates the guard after the startup stages, so a guardless pass can
        only re-verify read-only: a clear obligation waits for the next tick.
        """
        c = self.coordinator
        if c.authority.state is None or not c.native.inverters:
            return True
        mismatches: set[str] = set()
        try:
            async with c.control.lock:
                if c.guard is None:
                    async with c.native.lock:
                        for inverter in c.native.inverters.values():
                            await self._async_reconcile_preserve(inverter, now, mismatches)
                    return True
                async with c.guard.lock:
                    async with c.native.lock:
                        for inverter in c.native.inverters.values():
                            if strict:
                                if not await self._async_reconcile_one(
                                        inverter, now, mismatches):
                                    return False
                            else:
                                await self._async_reconcile_pending(
                                    inverter, now, mismatches)
        finally:
            for mismatched in mismatches:
                await c.native.async_readback_mismatch(mismatched)
        return True

    async def _async_reconcile_preserve(self, inverter: Inverter, now: datetime,
                                        mismatches: set[str]) -> bool:
        """Read Slot 1 and preserve an exact match; never write, never re-arm."""
        c = self.coordinator
        state = c.authority.state
        if state is None:
            return False
        record = latest_record(state.export_records, inverter.id)
        if record is None or record.state == "cleared":
            self.reverified.add(inverter.id)
            return True
        zone = c.control.household_zone()
        # Only a validated, non-caching device's Slot 1 can re-verify a record.
        if (zone is None or c.native.reason(inverter) is not None
                or record.epoch != state.epoch
                or record.store_id != state.store_id
                or record.event.end <= now.astimezone(UTC)
                or any(latch.event == record.event for latch in state.stop_latches)
                or not any(row.id == record.supervision_id and row.event == record.event
                           for row in state.export_supervisions)):
            return False
        from .native_slots import NativeSlots

        # A read is safe before the device is validated; only the clear waits.
        resident = await NativeSlots(self, inverter, state.epoch, mismatches).read(SLOT1_REG)
        if (resident is None
                or resident[4:] != clock_face(record.window.start, record.window.end, zone)):
            return False
        saved = await self._save_export_record(inverter.id, record.model_copy(
            update={"state": "preserved", "last_reconcile_at": now}), now)
        if not saved:
            return False
        self.reverified.add(inverter.id)
        return True

    async def _async_reconcile_one(self, inverter: Inverter, now: datetime,
                                   mismatches: set[str]) -> bool:
        """One inverter's restart re-verification; the caller holds all three locks."""
        c = self.coordinator
        state = c.authority.state
        if state is None:
            return False
        record = latest_record(state.export_records, inverter.id)
        if record is None or record.state == "cleared":
            self.reverified.add(inverter.id)
            return True
        if state.fallback.active:
            # Fallback entry's cancel (with slots 2/3) is retried by the cancel loop;
            # a record is never live while fallback is active.
            return True
        if await self._async_reconcile_preserve(inverter, now, mismatches):
            return True
        if c.native.reason(inverter) is not None:
            # An unvalidated device is never written: the record stays unverified
            # and is retried once the device validates.
            return True
        result = await self._async_clear_held(
            inverter.id, epoch=state.epoch, now=now, transition=False, cleanup=True,
            removal=False, mismatches=mismatches)
        if result.outcome not in ("applied", "unchanged"):
            return False
        self.reverified.add(inverter.id)
        return True

    async def _async_reconcile_pending(self, inverter: Inverter, now: datetime,
                                       mismatches: set[str]) -> None:
        """Re-verification for a record the startup stage could not settle yet."""
        if inverter.id in self.reverified:
            return
        await self._async_reconcile_one(inverter, now, mismatches)

    def _export_policy_reason(self, inverter: Inverter, epoch: int) -> WriteReason | None:
        """Export is Optimise-only with no manual session, fallback or other transition."""
        state = self.coordinator.authority.state
        if state is not None and (
                state.committed_mode != "optimise" or state.manual_session is not None
                or state.fallback.active
                or state.transition is not None and state.transition.target != "optimise"):
            return "mode"
        return self._refusal(inverter, epoch)

    def _live_record(self, inverter_id: str, now: datetime) -> ExportRecord | None:
        state = self.coordinator.authority.state
        if state is None:
            return None
        record = latest_record(state.export_records, inverter_id)
        if record is None or not record_is_live(
                record, now=now, epoch=state.epoch, store_id=state.store_id,
                supervisions=state.export_supervisions, stop_latches=state.stop_latches,
                fallback_active=state.fallback.active):
            return None
        if inverter_id not in self.armed_this_session and inverter_id not in self.reverified:
            # A record loaded from disk is never live before its re-verification.
            return None
        return record

    async def _save_export_record(self, inverter_id: str, record: ExportRecord | None,
                                  now: datetime, *, event: EventIdentity | None = None,
                                  kind: native_export_log.TransitionKind | None = None,
                                  reason: str | None = None,
                                  cleared: bool | None = None) -> bool:
        state = self.coordinator.authority.state
        if state is None:
            return False
        records = tuple(row for row in state.export_records
                        if row.inverter_id != inverter_id)
        if record is not None:
            records += (record,)
            if record.state != "cleared":
                self.armed_this_session.add(inverter_id)
        candidate = state.model_copy(update={
            "export_records": records, "last_changed_at": now,
        })
        if event is not None and kind is not None:
            candidate = native_export_log.with_append(
                candidate, inverter_id=inverter_id, event=event, kind=kind, at=now,
                reason=reason, cleared=cleared)
        return await self.coordinator.authority.async_save(candidate)

    async def _save_transition(self, inverter_id: str, event: EventIdentity | None,
                               kind: native_export_log.TransitionKind, now: datetime, *,
                               reason: str | None = None,
                               cleared: bool | None = None) -> None:
        """Best effort: a log save never changes the result of the write it describes."""
        if event is None:
            return
        state = self.coordinator.authority.state
        if state is None:
            return
        candidate = native_export_log.with_append(
            state, inverter_id=inverter_id, event=event, kind=kind, at=now,
            reason=reason, cleared=cleared)
        if candidate is not state:
            await self.coordinator.authority.async_save(candidate)

    async def _save_abort(self, inverter_id: str, event: EventIdentity | None,
                          now: datetime, reason: str, *, cleared: bool) -> None:
        await self._save_transition(inverter_id, event, "aborted", now,
                                    reason=reason, cleared=cleared)

    async def _log_refusal(self, inverter_id: str, event: EventIdentity | None,
                           now: datetime, reason: str | None, *, cleared: bool,
                           held: bool = False) -> None:
        """Deferral and untrusted holds never abort; the clock hold is retryable.

        ``held`` marks an arm write that did not read back: before the event
        starts it is the retryable ``unverified`` (#406), at or after it an abort.
        """
        if reason is None or reason == "hold_data_untrusted":
            return
        if reason == "clock_not_trusted":
            await self._save_transition(inverter_id, event, "clock_hold", now, reason=reason)
            return
        if held and event is not None and now < event.start:
            await self._save_transition(inverter_id, event, "unverified", now,
                                        reason="unverified", cleared=cleared)
            return
        await self._save_abort(inverter_id, event, now, reason, cleared=cleared)

    def _latest_event(self, inverter_id: str) -> EventIdentity | None:
        state = self.coordinator.authority.state
        if state is None:
            return None
        record = latest_record(state.export_records, inverter_id)
        return None if record is None else record.event

    async def _mark_cleared(self, inverter_id: str, now: datetime) -> bool:
        """A verified clear is the only thing that retires a record."""
        state = self.coordinator.authority.state
        if state is None:
            return False
        records = list(state.export_records)
        found = False
        cleanup_event: EventIdentity | None = None
        for index, row in enumerate(records):
            if row.inverter_id == inverter_id and row.state != "cleared":
                if row.state in ("armed", "active", "preserved", "stop_pending"):
                    cleanup_event = row.event
                records[index] = row.model_copy(update={
                    "state": "cleared", "cleanup_verified": True,
                    "last_reconcile_at": now,
                })
                found = True
        if not found:
            return True
        candidate = state.model_copy(update={
            "export_records": tuple(records), "last_changed_at": now,
        })
        if cleanup_event is not None:
            candidate = native_export_log.with_append(
                candidate, inverter_id=inverter_id, event=cleanup_event,
                kind="cleanup_verified", at=now)
        return await self.coordinator.authority.async_save(candidate)

    @staticmethod
    def _same_event(first: EventIdentity, second: EventIdentity) -> bool:
        return (first.direction == second.direction and first.start == second.start
                and first.end == second.end)

    @staticmethod
    def _resolved_event(request: ExportWindow) -> EventIdentity:
        return EventIdentity(direction=request.event.direction, start=request.event.start,
                             end=request.event.end)

    def _hold_overlaps(self, inverter: Inverter, start: datetime, end: datetime,
                       now: datetime) -> bool:
        """A live hold record, or trusted dispatch evidence, beats export."""
        state = self.coordinator.authority.state
        if state is not None and any(
                hold.device.power_switch == inverter.id for hold in state.holds):
            return True
        evidence = self.coordinator.holds.evidence.read(now)
        return evidence.dispatch_trusted and any(
            window.start < end and start < window.end for window in evidence.dispatches)

    async def _untrusted_holds(self, slots: "NativeSlots", request: ExportWindow,
                               live: ExportRecord | None, now: datetime
                               ) -> tuple[bool, list[int] | None]:
        """Refuse a new window while hold data is untrusted; keep only named cases."""
        c = self.coordinator
        evidence = c.holds.evidence.read(now)
        configured = c.holds.evidence.configured
        state = c.authority.state
        degraded = state is not None and any(
            hold.device.power_switch == request.inverter_id
            and hold.evidence_stale_since is not None for hold in state.holds)
        if configured and evidence.dispatch_trusted and not degraded:
            return False, None
        half = clock_face(request.window.start, request.window.end,
                          c.control.household_zone() or UTC)
        resident = await slots.read(SLOT1_REG)
        if resident is not None and resident[4:] == half:
            return False, None
        if (resident is not None and any(resident[4:]) and live is not None
                and live.window.end > now.astimezone(UTC)
                and resident[4:] == clock_face(live.window.start, live.window.end,
                                               c.control.household_zone() or UTC)):
            return True, resident[4:]
        return True, None

    def _clock_refused(self, inverter: Inverter, now: datetime) -> bool:
        zone = self.coordinator.control.household_zone()
        if zone is None:
            return True
        reading = self.coordinator.native.reading(inverter, native_write.CLOCK_FRAME)
        if reading is None or reading.registers is None:
            return True
        age = now - reading.read_at
        if age > STALE_AFTER or age < timedelta(seconds=-5):
            return True
        error = clock_error(reading.registers, reading.read_at, zone)
        return error is None or abs(error) > DRIFT_TOLERANCE

    async def _power_on(self, inverter: Inverter, now: datetime) -> bool:
        """Read the switch, never write it: ordinary export apply never turns it On."""
        transport = self.coordinator.native.transport(inverter)
        epoch = transport.connection_epoch
        read = await transport.read_holding(
            POWER_SWITCH_REG, 1, device_id=inverter.gateway_unit_id)
        if (read.error is not None or len(read.registers) != 1
                or not valid_words(read.registers)
                or epoch != transport.connection_epoch):
            return False
        self.coordinator.native.readings.setdefault(inverter.id, {})[
            POWER_SWITCH_FRAME] = Reading(dt_util.utcnow(), tuple(read.registers), epoch)
        return decode_power(list(read.registers)) == "on"

    async def _bound_reason(self, inverter: Inverter, supervision: ExportSupervision,
                            request: ExportWindow, now: datetime) -> WriteReason | None:
        """Re-verify identity, claim and sum right before the window is armed."""
        c = self.coordinator
        topology = c.guard.topology if c.guard is not None else None
        if topology is None:
            return "export_bound_unverified"
        limits = effective_limits(topology)
        transport = c.native.transport(inverter)
        epoch = transport.connection_epoch
        words: list[int] = []
        for address in (MODEL_REG, INVERTER_TYPE_REG, TOU_VERSION_REG):
            read = await transport.read_input(
                address, 1, device_id=inverter.gateway_unit_id)
            if (read.error is not None or len(read.registers) != 1
                    or not valid_words(read.registers)):
                return "export_bound_unverified"
            words.append(read.registers[0])
        if epoch != transport.connection_epoch:
            return "export_bound_unverified"
        if c.native.reason(inverter) is not None:
            return "export_bound_unverified"
        claims = c.entry.options.get("gateway_claims")
        raw_claims = claims if isinstance(claims, list) else []
        claim = next((claim_from_raw(raw) for raw in raw_claims
                      if isinstance(raw, dict)
                      and raw.get("host") == inverter.gateway_host
                      and raw.get("port") == inverter.gateway_port
                      and raw.get("unit_id") == inverter.gateway_unit_id), None)
        if claim != supervision.claim:
            return "export_bound_unverified"
        banks = {bank.id: bank for bank in topology.banks}
        connection_id = banks[inverter.bank_id].connection_id
        state = c.authority.state
        same_event = (state.export_supervisions if state is not None else ())
        # Every inverter on the connection still has a bound for this event and
        # is still validated: an unrated or invalid peer's output is unknown.
        for member in topology.inverters:
            if banks[member.bank_id].connection_id != connection_id:
                continue
            if (supervision_for(same_event, member.id, supervision.event) is None
                    or member.transport != "native" or c.native.reason(member) is not None):
                return "export_bound_unverified"
        total = sum(row.rating_w for row in same_event
                    if row.event == supervision.event
                    and banks[next(i.bank_id for i in topology.inverters
                                   if i.id == row.inverter_id)].connection_id == connection_id)
        reason = bound_refusal(
            supervision,
            DeviceExportFact(True, (words[0], words[1], words[2]), claim),
            site_limit_w=site_export_limit_w(topology, limits, connection_id),
            connection_sum_w=total)
        if reason is not None:
            return cast(WriteReason, reason)
        # A stale event or window never arms, even when the bound still holds.
        if request.window.end <= now.astimezone(UTC) or request.event.end <= now.astimezone(UTC):
            return "invalid_intent"
        return None

    async def _read_export_current(self, inverter: Inverter) -> list[int] | None:
        result = await self.coordinator.native.transport(inverter).read_holding(
            EXPORT_CURRENT_REG, 1, device_id=inverter.gateway_unit_id)
        if (result.error is not None or len(result.registers) != 1
                or not valid_words(result.registers)):
            return None
        return list(result.registers)

    async def _async_export_current(self, inverter: Inverter, epoch: int,
                                   mismatches: set[str], event: EventIdentity) -> IntentResult:
        """Write and confirm the fixed 62.5 A export command."""
        target = [native_write.current_word(EXPORT_CURRENT_A)]
        resident = await self._read_export_current(inverter)
        if resident is None:
            return IntentResult(outcome="failed")
        if resident == target:
            if await confirm(target, lambda: self._read_export_current(inverter),
                             native_write.verify_sleep, changed=False,
                             mismatch=lambda: mismatches.add(inverter.id)):
                return IntentResult(outcome="unchanged", written_a=EXPORT_CURRENT_A)
        reason = self._export_write_reason(inverter, epoch, event)
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        result = await self.coordinator.native.transport(inverter).write(
            EXPORT_CURRENT_REG, target, device_id=inverter.gateway_unit_id)
        if not result.ok:
            return IntentResult(outcome="failed")
        verified = await confirm(
            target, lambda: self._read_export_current(inverter), native_write.verify_sleep,
            changed=True, mismatch=lambda: mismatches.add(inverter.id))
        return IntentResult(outcome="applied" if verified else "unverified",
                            written_a=EXPORT_CURRENT_A if verified else None)

    async def _cleanup_locked(self, inverter: Inverter, slots: "NativeSlots",
                              now: datetime, *, zero_slots: bool = False) -> IntentResult:
        """Clear and zero-confirm the discharge half, retiring the record on success.

        A transition cancel also zeroes and verifies slots 2/3, as the oracle's
        ``cancel_export`` does; the record is retired only when all of it verifies.
        """
        result = await slots.clear_discharge()
        if result.outcome not in ("applied", "unchanged"):
            return result
        if zero_slots:
            for address in (SLOT2_REG, SLOT3_REG):
                extra = await slots.block(address, [0] * 8)
                if extra.outcome not in ("applied", "unchanged"):
                    return IntentResult(
                        outcome="failed" if extra.outcome == "failed" else "unverified")
        return await self._retire_after_zero(inverter, slots, now, result)

    async def _retire_after_zero(self, inverter: Inverter, slots: "NativeSlots",
                                 now: datetime, result: IntentResult | None = None
                                 ) -> IntentResult:
        """The discharge half alone must read zero first time and on 5 further reads."""
        async def half() -> list[int] | None:
            block = await slots.read(SLOT1_REG)
            return None if block is None else block[4:]
        if not slots.zero_half_confirmed and not await confirm(
                [0] * 4, half, native_write.verify_sleep, changed=True, mismatch=slots.mismatch):
            return IntentResult(outcome="unverified")
        if not await self._mark_cleared(inverter.id, now):
            return IntentResult(outcome="unverified")
        return result or IntentResult(outcome="unchanged")

    async def _drop_supervisions(self, inverter_id: str, now: datetime) -> bool:
        state = self.coordinator.authority.state
        if state is None:
            return False
        kept = tuple(row for row in state.export_supervisions if row.inverter_id != inverter_id)
        if len(kept) == len(state.export_supervisions):
            return True
        return await self.coordinator.authority.async_save(state.model_copy(update={
            "export_supervisions": kept, "last_changed_at": now}))

    async def _async_export_group(
        self, entries: list[tuple[str, ChargeCurrentLimit | ChargeWindow | ExportNone
                                 | ExportUntrusted | ExportWindow, int]],
        *, epoch: int, mismatches: set[str], fixed: str | None, malformed: bool = False,
        malformed_event: EventIdentity | None = None,
    ) -> dict[int, IntentResult]:
        """Export state changes serialise with every other authority save (control.lock)."""
        async with self.coordinator.control.lock:
            return await self._async_export_group_controlled(
                entries, epoch=epoch, mismatches=mismatches, fixed=fixed, malformed=malformed,
                malformed_event=malformed_event)

    def _export_write_reason(self, inverter: Inverter, epoch: int,
                             event: EventIdentity) -> WriteReason | None:
        """Write-time export permission: mode, device, a live supervision and no latch."""
        reason = self._export_policy_reason(inverter, epoch)
        if reason is not None:
            return reason
        state = self.coordinator.authority.state
        if state is None:
            return "authority_unknown"
        # A trip drops the bound with its latch, so the latch is judged first.
        if any(latch.event == event for latch in state.stop_latches):
            return "tripped"
        supervision = supervision_for(state.export_supervisions, inverter.id, event)
        if supervision is None or supervision.ends_at <= dt_util.utcnow():
            return "not_supervised"
        return None

    async def _async_export_group_controlled(
        self, entries: list[tuple[str, ChargeCurrentLimit | ChargeWindow | ExportNone
                                 | ExportUntrusted | ExportWindow, int]],
        *, epoch: int, mismatches: set[str], fixed: str | None, malformed: bool,
        malformed_event: EventIdentity | None = None,
    ) -> dict[int, IntentResult]:
        """One guard-lock transaction per inverter, in the oracle's export order."""
        from .native_slots import NativeSlots

        c = self.coordinator
        guard = c.guard
        results: dict[int, IntentResult] = {}
        export_item = next(((kind, intent, index) for kind, intent, index in entries
                            if kind == "export"), None)
        if export_item is None or guard is None:
            return {index: IntentResult(outcome="refused", reason="limits")
                    for _, _, index in entries}
        _, raw, export_index = export_item
        assert isinstance(raw, (ExportNone, ExportUntrusted, ExportWindow))
        request = raw
        inverter = next((i for i in guard.topology.inverters
                         if i.id == request.inverter_id), None)
        if inverter is None:
            return {index: IntentResult(outcome="refused", reason="limits")
                    for _, _, index in entries}
        now = dt_util.utcnow()
        async with guard.lock:
            async with c.native.lock:
                state = c.authority.state
                if state is None:
                    return {index: IntentResult(outcome="refused", reason="authority_unknown")
                            for _, _, index in entries}
                # A record loaded from disk is re-verified before any apply uses it.
                await self._async_reconcile_pending(inverter, now, mismatches)
                state = c.authority.state
                if state is None:
                    return {index: IntentResult(outcome="refused", reason="authority_unknown")
                            for _, _, index in entries}
                live = self._live_record(inverter.id, now)
                # 1. An untrusted event read with a live verified record skips the
                # whole inverter pass: no reads of Slot 1 and no writes at all.
                if isinstance(request, ExportUntrusted) and live is not None:
                    return {index: IntentResult(outcome="unchanged")
                            for _, _, index in entries}
                ready = False
                deferred = False
                keep = False
                failure: Literal["unverified", "failed"] | None = None
                # The arm itself did not read back (#406): retryable before the start.
                arm_unverified = False
                arm_failed_slot = False
                half = [0, 0, 0, 0]
                refusal: WriteReason | None = None
                event: EventIdentity | None = malformed_event
                supervision: ExportSupervision | None = None
                if isinstance(request, ExportWindow):
                    event = self._resolved_event(request)
                    supervision = supervision_for(
                        state.export_supervisions, inverter.id, event)
                    zone = c.control.household_zone()
                    refusal = self._export_policy_reason(inverter, epoch)
                    if refusal is None:
                        if any(latch.event == event for latch in state.stop_latches):
                            # A trip drops the bound, so the latch is judged first.
                            refusal = "tripped"
                        elif supervision is None or not self._same_event(
                                supervision.event, event):
                            refusal = "not_supervised"
                        elif (request.window.end <= now.astimezone(UTC)
                              or request.event.end <= now.astimezone(UTC)
                              or request.window.start < request.event.start
                              or request.window.end > request.event.end):
                            # The selected window must lie inside the supervised event.
                            refusal = "invalid_intent"
                        elif (not request.soc.ok or not hasattr(c, "soc")
                              or not c.soc.trusted(inverter.bank_id)):
                            # The SoC gate never writes (oracle's early BLOCKED).
                            await self._save_abort(inverter.id, event, now, "untrusted_soc",
                                                   cleared=False)
                            return {index: IntentResult(outcome="refused",
                                                        reason="untrusted_soc")
                                    for _, _, index in entries}
                        elif fixed is not None:
                            refusal = "limits"
                        elif zone is None:
                            refusal = "clock_not_trusted"
                    if refusal is None:
                        assert zone is not None
                        half = clock_face(request.window.start, request.window.end, zone)
                        if export_not_yet_armed(
                                request.window.start.astimezone(zone),
                                request.window.end.astimezone(zone), now.astimezone(zone)):
                            deferred = True
                            if live is not None and self._same_event(live.event, event):
                                resident = await NativeSlots(
                                    self, inverter, epoch, mismatches).read(SLOT1_REG)
                                keep = resident is not None and resident[4:] == half
                        else:
                            if self._hold_overlaps(
                                    inverter, request.window.start,
                                    request.window.end, now):
                                refusal = "hold_overlaps_export"
                            else:
                                untrusted, kept = await self._untrusted_holds(
                                    NativeSlots(self, inverter, epoch, mismatches),
                                    request, live, now)
                                if untrusted:
                                    refusal = "hold_data_untrusted"
                                    if kept is not None:
                                        keep = True
                                        half = kept
                                elif not (live is not None
                                          and self._same_event(live.event, event)):
                                    if self._clock_refused(inverter, now):
                                        refusal = "clock_not_trusted"
                                if refusal is None and not await self._power_on(
                                        inverter, now):
                                    refusal = "power_switch_not_on"
                                if refusal is None:
                                    assert supervision is not None
                                    refusal = await self._bound_reason(
                                        inverter, supervision, request, now)
                                if refusal is None:
                                    ready = True
                slots = NativeSlots(self, inverter, epoch, mismatches)
                # Every write carrying a discharge half re-checks export permission
                # with no await before the write (the slots' own recheck point).
                check_event = live.event if keep and live is not None else event
                if check_event is not None:
                    def check(checked: EventIdentity = check_event) -> WriteReason | None:
                        return self._export_write_reason(inverter, epoch, checked)
                    slots.discharge_check = check
                if isinstance(request, ExportNone) and not malformed:
                    # A trusted "none" ends authority to export before the clear.
                    if not await self._drop_supervisions(inverter.id, now):
                        return {index: IntentResult(outcome="refused",
                                                    reason="authority_unknown")
                                for _, _, index in entries}
                written: list[IntentResult] = []
                if ready:
                    assert (isinstance(request, ExportWindow) and event is not None
                            and supervision is not None)
                    record = ExportRecord(
                        inverter_id=inverter.id, event=event,
                        window=StoredWindow(start=request.window.start,
                                            end=request.window.end),
                        store_id=state.store_id, epoch=state.epoch,
                        supervision_id=supervision.id, state="unknown",
                        last_reconcile_at=now,
                    )
                    # 10a. Write-ahead: a failed save writes nothing at all.
                    if not await self._save_export_record(inverter.id, record, now):
                        return {
                            index: IntentResult(outcome="refused", reason="authority_unknown")
                            for _, _, index in entries}
                    assert event is not None
                    current = await self._async_export_current(
                        inverter, epoch, mismatches, event)
                    written.append(current)
                    if current.outcome not in ("applied", "unchanged"):
                        ready = False
                        refusal = current.reason
                        failure = ("failed" if current.outcome == "failed"
                                   else "unverified")
                        arm_unverified = current.outcome == "unverified"
                planned = half if ready or keep else [0, 0, 0, 0]
                charge_window = False
                for kind, intent, index in entries:
                    if kind == "export":
                        continue
                    if isinstance(intent, ChargeWindow):
                        charge_window = True
                        slots.planned_discharge = planned
                        result = await self._async_held(guard, intent, epoch, mismatches,
                                                        slots=slots)
                    else:
                        assert isinstance(intent, ChargeCurrentLimit)
                        result = await self._async_held(guard, intent, epoch, mismatches)
                    results[index] = result
                slot_result = slots.block_result
                if ready and not charge_window:
                    slot_result = await slots.arm(half)
                elif not ready and not keep and (
                        not charge_window or slot_result is None
                        or slot_result.outcome == "refused"):
                    slot_result = await self._cleanup_locked(inverter, slots, now)
                elif not ready and not keep and slot_result is not None:
                    if slot_result.outcome in ("applied", "unchanged"):
                        # The charge window wrote a zero half: confirm it as a zero.
                        slot_result = await self._retire_after_zero(inverter, slots, now)
                if ready and (slot_result is None or slot_result.outcome in (
                        "unverified", "failed", "refused")):
                    refusal = slot_result.reason if slot_result is not None else None
                    failure = (slot_result.outcome if slot_result is not None
                               and slot_result.outcome in ("unverified", "failed") else None)
                    # The failed arm write is this pass's slot result: no cleanup ran.
                    arm_unverified = failure == "unverified"
                    arm_failed_slot = True
                    ready = False
                if ready:
                    assert isinstance(request, ExportWindow)
                    assert event is not None
                    active = now.astimezone(UTC) >= request.window.start.astimezone(UTC)
                    saved = await self._save_export_record(inverter.id, record.model_copy(
                        update={"state": "active" if active else "armed",
                                "cleanup_verified": False,
                                "last_reconcile_at": now}), now,
                        event=event, kind="started" if active else "armed")
                    if not saved:
                        results[export_index] = IntentResult(outcome="unverified")
                    else:
                        applied = (written and written[0].outcome == "applied") or (
                            slot_result is not None and slot_result.outcome == "applied")
                        results[export_index] = IntentResult(
                            outcome="applied" if applied else "unchanged",
                            written_a=EXPORT_CURRENT_A if applied else None)
                    return results
                cleanup = IntentResult(outcome="unchanged")
                if not keep:
                    cleanup = (slot_result if slot_result is not None
                               else await self._cleanup_locked(inverter, slots, now))
                    if cleanup.outcome in ("unverified", "failed"):
                        # A failed cleanup is terminal; only the failed arm's own
                        # unread window stays retryable.
                        await self._log_refusal(
                            inverter.id, event or self._latest_event(inverter.id), now,
                            None if deferred else ("invalid_intent" if malformed else
                                                   failure or refusal or cleanup.outcome),
                            cleared=False, held=arm_unverified and arm_failed_slot)
                        results[export_index] = IntentResult(outcome="unverified")
                        return results
                if malformed:
                    results[export_index] = IntentResult(outcome="refused",
                                                         reason="invalid_intent")
                elif failure is not None:
                    results[export_index] = IntentResult(outcome=failure)
                elif deferred:
                    results[export_index] = IntentResult(outcome="deferred")
                elif refusal is not None:
                    results[export_index] = IntentResult(
                        outcome="refused", reason=refusal)
                else:
                    results[export_index] = IntentResult(
                        outcome=cleanup.outcome, reason=cleanup.reason)
                if not keep and not deferred:
                    await self._log_refusal(
                        inverter.id, event or self._latest_event(inverter.id), now,
                        "invalid_intent" if malformed else failure or refusal,
                        cleared=cleanup.outcome in ("applied", "unchanged"),
                        held=arm_unverified and cleanup.outcome in ("applied", "unchanged"))
                return results

    async def async_cancel_export(self, inverter_id: str, *, epoch: int,
                                  removal: bool = False) -> IntentResult:
        """Clear the discharge half under a transition's own write policy."""
        return await self.async_clear_export(inverter_id, epoch=epoch, transition=True,
                                             removal=removal)

    async def async_clear_export(self, inverter_id: str, *, epoch: int,
                                 transition: bool = False, cleanup: bool = False,
                                 removal: bool = False) -> IntentResult:
        """Clear and zero-confirm the discharge half, retiring the record on success."""
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        mismatches: set[str] = set()
        now = dt_util.utcnow()
        try:
            async with guard.lock:
                async with self.coordinator.native.lock:
                    return await self._async_clear_held(
                        inverter_id, epoch=epoch, now=now, transition=transition,
                        cleanup=cleanup, removal=removal, mismatches=mismatches)
        finally:
            for mismatched in mismatches:
                await self.coordinator.native.async_readback_mismatch(mismatched)

    async def _async_clear_held(self, inverter_id: str, *, epoch: int, now: datetime,
                                transition: bool, cleanup: bool, removal: bool,
                                mismatches: set[str]) -> IntentResult:
        """The whole clear; the caller holds guard.lock then native.lock."""
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        inverter = next((i for i in guard.topology.inverters if i.id == inverter_id), None)
        if inverter is None:
            return IntentResult(outcome="refused", reason="limits")
        reason = self._refusal(inverter, epoch, transition=transition, cleanup=cleanup)
        if reason is not None:
            if removal and (not self.coordinator._running or self.coordinator.native.closed):
                # A removal after unload cannot write: the obligation stays and
                # its abort is visible once the store has loaded again.
                await self._save_abort(inverter_id, self._latest_event(inverter_id), now,
                                       reason, cleared=False)
            return IntentResult(outcome="refused", reason=reason)
        from .native_slots import NativeSlots

        slots = NativeSlots(self, inverter, epoch, mismatches,
                            transition=transition, cleanup=cleanup)
        event = self._latest_event(inverter_id)
        result = await self._cleanup_locked(inverter, slots, now, zero_slots=transition)
        if result.outcome in ("unverified", "failed"):
            # A cancel whose clear or zero confirm failed is a terminal abort.
            await self._save_abort(inverter_id, event, now, result.outcome, cleared=False)
        return result
