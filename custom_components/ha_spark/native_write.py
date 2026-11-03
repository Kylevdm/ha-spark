"""One serialised, clamped and verified native charge-current boundary.

Lock order is guard then native bus. No caller-supplied SoC exemption exists.
Plan current limits, charge windows and guard ceilings share this boundary.
The guard policy permits only current changes, never window activation.
Resident state is evidence only after exact read-back, never a requested value.
"""
import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from math import floor
from typing import TYPE_CHECKING, Literal, cast

from homeassistant.util import dt as dt_util
from pydantic import ValidationError

from . import native_export_log
from .authority_store import EventIdentity, ExportRecord, ExportSupervision
from .authority_store import ExportWindow as StoredWindow
from .clamp import DeviceIntent, clamp_intent
from .clock_sync import DRIFT_TOLERANCE, STALE_AFTER, clock_error
from .commands import (
    ApplyPayload,
    ChargeCurrentLimit,
    ChargeWindow,
    Command,
    CommandResult,
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
    event_from_raw,
    export_not_yet_armed,
    fixed_export_refusal,
    latest_record,
    record_is_live,
    site_export_limit_w,
    supervision_for,
)
from .native_power import POWER_SWITCH_REG, decode_power
from .native_verify import confirm
from .solis_frames import Reading
from .solis_transport import Transport, valid_words
from .topology import Inverter, effective_limits, validate_topology

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .guard_loop import SupplyGuardLoop
    from .native_slots import NativeSlots

verify_sleep = asyncio.sleep
_SEVERITY = ("applied", "unchanged", "deferred", "refused", "unverified", "failed")


def current_word(current: float) -> int:
    """Tenths of an amp, floored so a write is never above the clamped request."""
    return floor(round(max(current, 0.0) * 10, 6))


class NativeChargeBoundary:
    """Shared guard/bus transactions with caller-specific authority policies."""

    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.written_a: dict[str, float] = {}
        self.written_at: dict[str, datetime] = {}
        self.verified_windows: dict[str, tuple[int, tuple[int, ...]]] = {}
        self.verified_fallback: dict[str, tuple[int, tuple[int, ...]]] = {}
        self.last_apply: tuple[int, datetime, bool] | None = None

    def plan_verified(self, epoch: int, *, since: datetime) -> bool:
        """A freshly accepted plan in this epoch, judged on charge intents only."""
        last = self.last_apply
        return last is not None and last[0] == epoch and last[1] >= since and last[2]

    async def async_barrier(self) -> None:
        """Wait for complete read-back transactions, without aborting any write."""
        guard = self.coordinator.guard
        if guard is not None:
            async with guard.lock:
                async with self.coordinator.native.lock:
                    pass
        else:
            async with self.coordinator.native.lock:
                pass

    def _refusal(self, inverter: Inverter, epoch: int, *,
                 guard_write: bool = False,
                 entity_control: str | None = None,
                 fallback: bool = False,
                 transition: bool = False) -> WriteReason | None:
        coordinator = self.coordinator
        state = coordinator.authority.state
        if state is None:
            return "authority_unknown"
        if not coordinator.control.owned():
            return "gateway_not_owned"
        if state.epoch != epoch:
            return "stale_epoch"
        if entity_control is not None:
            if not coordinator.control.ready or state.transition is not None:
                return "not_available"
            manual = state.manual_session
            if entity_control == "power":
                if (manual is not None and manual.keep_protection or manual is None
                        and (state.committed_mode != "observe" or state.fallback.active)):
                    return "mode"
            elif entity_control == "discharge":
                if manual is None:
                    return "mode"
            elif manual is None and (state.committed_mode == "optimise"
                                      or state.fallback.active):
                return "mode"
        elif guard_write:
            if (state.committed_mode not in ("protect", "optimise")
                    or state.transition is not None and state.transition.target == "observe"):
                return "mode"
        elif fallback:
            # A fallback-schedule write is also the owner's starting schedule on
            # the way out of Optimise; manual control never programs one.
            # In Optimise it needs a live fallback; leaving Optimise it is the
            # write_fallback step of a transition to Protect or Observe.
            leaving = (state.transition is not None
                       and state.transition.target in ("protect", "observe"))
            if (state.committed_mode != "optimise" or state.manual_session is not None
                    or not leaving and not state.fallback.active
                    or state.transition is not None and not leaving
                    and state.transition.target != "optimise"):
                return "mode"
        elif transition:
            # Transition-owned discharge cleanup, including manual start/fallback entry.
            if (state.committed_mode != "optimise"
                    or state.transition is None and not state.fallback.active):
                return "mode"
        elif state.fallback.active:
            # A plan writes during fallback only while every cause has cleared.
            if (state.fallback.causes or coordinator.fallback.retrying(dt_util.utcnow())
                    or state.committed_mode != "optimise"
                    or state.manual_session is not None
                    or state.transition is not None and state.transition.target != "optimise"):
                return "mode"
        elif (state.committed_mode != "optimise" or state.manual_session is not None
              or state.transition is not None and state.transition.target != "optimise"):
            return "mode"
        # Unloading fences new writes the moment it starts, before native closes.
        if (not coordinator._running or not coordinator.native.polling
                or coordinator.native.closed or coordinator.native.reason(inverter) is not None):
            return "read_only"
        guard = coordinator.guard
        if (guard is None or not guard.boundary_open
                or validate_topology(coordinator.entry.options.get("topology"))
                != guard.topology or guard.limits != effective_limits(guard.topology)):
            return "limits"
        return None

    async def async_entity(self, inverter_id: str, control: str, value: float | list[int],
                           *, epoch: int, offset: int | None = None) -> IntentResult:
        """Owner writes share the guard lock, clamp and verified commit path."""
        from .native_entity_write import async_entity

        return await async_entity(self, inverter_id, control, value, epoch=epoch, offset=offset)

    async def async_restoration_held(self, inverter: Inverter, *, epoch: int,
                                      handover: bool = False) -> bool:
        """Read-only hold-release seam; caller holds guard.lock then native.lock.

        Protect/manual leave the owner's charging edits untouched. Ordinary
        Optimise releases require the last fully verified window in this epoch.
        Fallback restoration remains refused until its validated schedule seam
        is supplied by the fallback controller.
        """
        c = self.coordinator
        guard = c.guard
        if (guard is None or not guard.boundary_open
                or validate_topology(c.entry.options.get("topology")) != guard.topology
                or guard.limits != effective_limits(guard.topology)):
            return False
        transport = c.native.transport(inverter)
        connection_epoch = getattr(transport, "connection_epoch", 0)
        read = await transport.read_holding(43141, 30, device_id=inverter.gateway_unit_id)
        words = read.registers
        if (read.error is not None or len(words) != 30 or not valid_words(words)
                or words[0] > 625 or connection_epoch != getattr(transport, "connection_epoch", 0)):
            return False
        try:
            time(words[2], words[3])
            time(words[4], words[5])
        except ValueError:
            return False
        state = c.authority.state
        if state is None or state.epoch != epoch:
            return False
        if not handover and state.manual_session is None and state.committed_mode == "optimise":
            if state.fallback.active:
                # Fallback restoration is read-only: the resident charge-only
                # schedule must equal the last fully verified fallback apply.
                if self.verified_fallback.get(inverter.id) != (
                        epoch, (words[0], *words[2:10])):
                    return False
            elif self.verified_windows.get(inverter.id) != (epoch, tuple(words[2:10])):
                return False
            if (self.written_a.get(inverter.id) != words[0] / 10
                    or any(words[12:20] + words[22:30])):
                return False
        c.native.readings[inverter.id]["holding_43141"] = Reading(
            dt_util.utcnow(), words, connection_epoch)
        return True

    async def async_reverify_windows(self, epoch: int) -> None:
        """Adopt the resident Slot 1 windows when a manual session ends.

        Read-only: a valid zero-discharge resident block becomes this epoch's
        verified window, so an ordinary Optimise release is not delayed.
        """
        c = self.coordinator
        state = c.authority.state
        guard = c.guard
        if (guard is None or state is None or state.epoch != epoch
                or state.committed_mode != "optimise" or state.manual_session is not None
                or state.fallback.active
                or state.transition is not None and state.transition.target != "optimise"
                or not c.control.owned() or not c._running or not c.native.polling
                or c.native.closed):
            return
        async with guard.lock:
            async with c.native.lock:
                def eligible() -> bool:
                    state = c.authority.state
                    return not (
                        state is None or state.epoch != epoch
                        or state.committed_mode != "optimise"
                        or state.manual_session is not None or state.fallback.active
                        or state.transition is not None and state.transition.target != "optimise"
                        or not c.control.owned() or not c._running or not c.native.polling
                        or c.native.closed)

                for inverter in c.native.inverters.values():
                    if not eligible():
                        return
                    if c.native.reason(inverter) is not None:
                        continue
                    transport = c.native.transport(inverter)
                    connection_epoch = getattr(transport, "connection_epoch", 0)
                    read = await transport.read_holding(
                        43141, 30, device_id=inverter.gateway_unit_id)
                    words = read.registers
                    if (read.error is not None or len(words) != 30
                            or not valid_words(words) or words[0] > 625
                            or connection_epoch != getattr(
                                transport, "connection_epoch", 0)):
                        continue
                    try:
                        time(words[2], words[3])
                        time(words[4], words[5])
                    except ValueError:
                        continue
                    if any(words[6:10] + words[12:20] + words[22:30]):
                        continue
                    # The read awaited: unloading or a mode change may have begun.
                    if not eligible():
                        return
                    if c.native.reason(inverter) is not None:
                        continue
                    self.verified_windows[inverter.id] = (epoch, tuple(words[2:10]))

    async def async_apply(self, command: Command) -> CommandResult:
        """Isolate intents and aggregate the worst positional result."""
        # Freshness is judged from acceptance, not completion.
        accepted_at = dt_util.utcnow()
        raw_intents = ApplyPayload.model_validate(command.payload).intents
        schemas: dict[str, type[ChargeCurrentLimit] | type[ChargeWindow] | type[ExportNone]
                     | type[ExportUntrusted] | type[ExportWindow]] = {
            "charge_current_limit": ChargeCurrentLimit, "charge_window": ChargeWindow,
            "export": ExportNone}
        results: list[IntentResult | None] = [None] * len(raw_intents)
        kinds: list[str | None] = []
        malformed: set[str] = set()
        malformed_events: dict[str, EventIdentity | None] = {}
        parsed: list[tuple[str, ChargeCurrentLimit | ChargeWindow | ExportNone
                           | ExportUntrusted | ExportWindow, int]] = []
        for index, raw in enumerate(raw_intents):
            kind = raw.get("kind")
            kinds.append(kind if isinstance(kind, str) else None)
            if not isinstance(kind, str) or kind not in schemas:
                results[index] = IntentResult(outcome="refused", reason="not_available")
                continue
            model = (
                ExportWindow if kind == "export" and raw.get("request") == "window"
                else ExportUntrusted if kind == "export" and raw.get("request") == "untrusted"
                else schemas[kind])
            try:
                intent = model.model_validate(raw)
            except ValidationError:
                results[index] = IntentResult(outcome="refused", reason="invalid_intent")
                inverter_id = raw.get("inverter_id")
                if (kind == "export" and isinstance(inverter_id, str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", inverter_id)):
                    # A malformed export still clears its inverter's discharge half.
                    malformed.add(inverter_id)
                    malformed_events[inverter_id] = event_from_raw(raw.get("event"))
                    parsed.append((kind, ExportNone(kind="export", inverter_id=inverter_id,
                                                    request="none"), index))
                continue
            parsed.append((kind, intent, index))
        guard = self.coordinator.guard
        window_ids = [intent.inverter_id for kind, intent, _ in parsed
                      if kind == "export" and isinstance(intent, ExportWindow)]
        fixed: dict[str, str | None] = {}
        if guard is not None and window_ids:
            banks = {bank.id: bank for bank in guard.topology.banks}
            inverters = {inverter.id: inverter for inverter in guard.topology.inverters}
            by_bank: dict[str, list[str]] = {}
            for inverter_id in window_ids:
                inverter = inverters.get(inverter_id)
                if inverter is not None:
                    by_bank.setdefault(banks[inverter.bank_id].id, []).append(inverter_id)
            for bank_id, members in by_bank.items():
                fixed[bank_id] = fixed_export_refusal(members, guard.topology, guard.limits)
        groups: dict[str, list[tuple[str, ChargeCurrentLimit | ChargeWindow | ExportNone
                                     | ExportUntrusted | ExportWindow, int]]] = {}
        for kind, intent, index in parsed:
            groups.setdefault(intent.inverter_id, []).append((kind, intent, index))
        for inverter_id, entries in groups.items():
            if any(kind == "export" for kind, _, _ in entries):
                inverter = (next((i for i in guard.topology.inverters if i.id == inverter_id),
                                 None) if guard is not None else None)
                reason = None if inverter is None else fixed.get(inverter.bank_id)
                if inverter is None:
                    reason = "limits"
                mismatches: set[str] = set()
                try:
                    group = await self._async_export_group(
                        entries, epoch=command.epoch, mismatches=mismatches, fixed=reason,
                        malformed=inverter_id in malformed,
                        malformed_event=malformed_events.get(inverter_id))
                except Exception:
                    self.written_a.pop(inverter_id, None)
                    group = {index: IntentResult(outcome="failed")
                             for _, _, index in entries}
                finally:
                    for mismatched in mismatches:
                        await self.coordinator.native.async_readback_mismatch(mismatched)
                for index, result in group.items():
                    results[index] = result
                continue
            for _, intent, index in entries:
                if not isinstance(intent, (ChargeCurrentLimit, ChargeWindow)):
                    results[index] = IntentResult(
                        outcome="refused", reason="not_available")
                    continue
                try:
                    results[index] = await self.async_current(intent, epoch=command.epoch)
                except Exception:
                    # Fixed outcomes only; a broken device cannot skip its peers.
                    self.written_a.pop(intent.inverter_id, None)
                    results[index] = IntentResult(outcome="failed")
        final = [result or IntentResult(outcome="failed") for result in results]
        charge = [result for kind, result in zip(kinds, final, strict=False)
                  if kind in ("charge_current_limit", "charge_window")]
        if charge:
            self.last_apply = (command.epoch, accepted_at, all(
                result.outcome in ("applied", "unchanged") for result in charge))
        worst = max(final, key=lambda r: _SEVERITY.index(r.outcome)) if final else (
            IntentResult(outcome="refused", reason="invalid_intent"))
        return CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome=worst.outcome, reason=worst.reason, intents=final)

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
                           now: datetime, reason: str | None, *, cleared: bool) -> None:
        """Deferral and untrusted holds never abort; the clock hold is retryable."""
        if reason is None or reason == "hold_data_untrusted":
            return
        if reason == "clock_not_trusted":
            await self._save_transition(inverter_id, event, "clock_hold", now, reason=reason)
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
        resident = await slots.read(43143)
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
        reading = self.coordinator.native.reading(inverter, "input_33022")
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
        epoch = getattr(transport, "connection_epoch", 0)
        read = await transport.read_holding(
            POWER_SWITCH_REG, 1, device_id=inverter.gateway_unit_id)
        if (read.error is not None or len(read.registers) != 1
                or not valid_words(read.registers)
                or epoch != getattr(transport, "connection_epoch", 0)):
            return False
        self.coordinator.native.readings.setdefault(inverter.id, {})[
            "holding_43007"] = Reading(dt_util.utcnow(), tuple(read.registers), epoch)
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
        epoch = getattr(transport, "connection_epoch", 0)
        words: list[int] = []
        for address in (33000, 35000, 33289):
            read = await transport.read_input(
                address, 1, device_id=inverter.gateway_unit_id)
            if (read.error is not None or len(read.registers) != 1
                    or not valid_words(read.registers)):
                return "export_bound_unverified"
            words.append(read.registers[0])
        if epoch != getattr(transport, "connection_epoch", 0):
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
            43142, 1, device_id=inverter.gateway_unit_id)
        if (result.error is not None or len(result.registers) != 1
                or not valid_words(result.registers)):
            return None
        return list(result.registers)

    async def _async_export_current(self, inverter: Inverter, epoch: int,
                                   mismatches: set[str], event: EventIdentity) -> IntentResult:
        """Write and confirm the fixed 62.5 A export command."""
        target = [current_word(EXPORT_CURRENT_A)]
        resident = await self._read_export_current(inverter)
        if resident is None:
            return IntentResult(outcome="failed")
        if resident == target:
            if await confirm(target, lambda: self._read_export_current(inverter),
                             verify_sleep, changed=False, mismatch=lambda: mismatches.add(
                                 inverter.id)):
                return IntentResult(outcome="unchanged", written_a=EXPORT_CURRENT_A)
        reason = self._export_write_reason(inverter, epoch, event)
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        result = await self.coordinator.native.transport(inverter).write(
            43142, target, device_id=inverter.gateway_unit_id)
        if not result.ok:
            return IntentResult(outcome="failed")
        verified = await confirm(
            target, lambda: self._read_export_current(inverter), verify_sleep,
            changed=True, mismatch=lambda: mismatches.add(inverter.id))
        return IntentResult(outcome="applied" if verified else "unverified",
                            written_a=EXPORT_CURRENT_A if verified else None)

    async def _cleanup_locked(self, inverter: Inverter, slots: "NativeSlots",
                              now: datetime) -> IntentResult:
        """Clear and zero-confirm the discharge half, retiring the record on success."""
        result = await slots.clear_discharge()
        if result.outcome in ("applied", "unchanged"):
            return await self._retire_after_zero(inverter, slots, now, result)
        return result

    async def _retire_after_zero(self, inverter: Inverter, slots: "NativeSlots",
                                 now: datetime, result: IntentResult | None = None
                                 ) -> IntentResult:
        """The discharge half alone must read zero first time and on 5 further reads."""
        async def half() -> list[int] | None:
            block = await slots.read(43143)
            return None if block is None else block[4:]
        if not await confirm([0] * 4, half, verify_sleep, changed=True,
                             mismatch=slots.mismatch):
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
        supervision = supervision_for(state.export_supervisions, inverter.id, event)
        if supervision is None or supervision.ends_at <= dt_util.utcnow():
            return "not_supervised"
        if any(latch.event == event for latch in state.stop_latches):
            return "tripped"
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
                        if supervision is None or not self._same_event(
                                supervision.event, event):
                            refusal = "not_supervised"
                        elif (request.window.end <= now.astimezone(UTC)
                              or request.event.end <= now.astimezone(UTC)
                              or request.window.start < request.event.start
                              or request.window.end > request.event.end):
                            # The selected window must lie inside the supervised event.
                            refusal = "invalid_intent"
                        elif any(latch.event == event for latch in state.stop_latches):
                            refusal = "tripped"
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
                                    self, inverter, epoch, mismatches).read(43143)
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
                        await self._log_refusal(
                            inverter.id, event or self._latest_event(inverter.id), now,
                            None if deferred else ("invalid_intent" if malformed else
                                                   failure or refusal or cleanup.outcome),
                            cleared=False)
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
                        cleared=cleanup.outcome in ("applied", "unchanged"))
                return results

    async def async_fallback(self, inverter_id: str, *, current_a: float, start: str,
                             end: str, epoch: int) -> IntentResult:
        """Apply the validated fallback schedule through the one charge-write path."""
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        request = ChargeWindow(kind="charge_window", inverter_id=inverter_id,
                               current_a=current_a, start=start, end=end)
        mismatches: set[str] = set()
        try:
            async with guard.lock:
                async with self.coordinator.native.lock:
                    inverter = next((i for i in guard.topology.inverters
                                     if i.id == inverter_id), None)
                    if inverter is None:
                        return IntentResult(outcome="refused", reason="limits")
                    reason = self._refusal(inverter, epoch, fallback=True)
                    if reason is not None:
                        return IntentResult(outcome="refused", reason=reason)
                    from .native_slots import NativeSlots

                    slots = NativeSlots(self, inverter, epoch, mismatches, fallback=True)
                    previous = guard.request_current(inverter.id, request.current_a)
                    try:
                        result, keep = await slots.window(guard, request, soc_required=False)
                    except BaseException:
                        self._restore(guard, inverter.id, previous)
                        raise
                    if not keep:
                        self._restore(guard, inverter.id, previous)
                    return result
        finally:
            for mismatched in mismatches:
                await self.coordinator.native.async_readback_mismatch(mismatched)

    async def async_cancel_export(self, inverter_id: str, *, epoch: int) -> IntentResult:
        """Clear the discharge half under a transition's own write policy."""
        return await self.async_clear_export(inverter_id, epoch=epoch, transition=True)

    async def async_clear_export(self, inverter_id: str, *, epoch: int,
                                 transition: bool = False) -> IntentResult:
        """Clear and zero-confirm the discharge half, retiring the record on success."""
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        mismatches: set[str] = set()
        now = dt_util.utcnow()
        try:
            async with guard.lock:
                async with self.coordinator.native.lock:
                    inverter = next((i for i in guard.topology.inverters
                                     if i.id == inverter_id), None)
                    if inverter is None:
                        return IntentResult(outcome="refused", reason="limits")
                    reason = self._refusal(inverter, epoch, transition=transition)
                    if reason is not None:
                        return IntentResult(outcome="refused", reason=reason)
                    from .native_slots import NativeSlots

                    slots = NativeSlots(self, inverter, epoch, mismatches,
                                        transition=transition)
                    event = self._latest_event(inverter.id)
                    result = await self._cleanup_locked(inverter, slots, now)
                    if result.outcome in ("unverified", "failed"):
                        # A cancel whose clear or zero confirm failed is a terminal abort.
                        await self._save_abort(inverter.id, event, now, result.outcome,
                                               cleared=False)
                    return result
        finally:
            for mismatched in mismatches:
                await self.coordinator.native.async_readback_mismatch(mismatched)

    async def async_write(self, intent: DeviceIntent) -> None:
        """Plan-only ChargeWriter seam; guard callers use the locked entry."""
        state = self.coordinator.authority.state
        if state is not None:
            await self.async_current(ChargeCurrentLimit(
                kind="charge_current_limit", inverter_id=intent.inverter_id,
                current_a=intent.charge_a), epoch=state.epoch)

    async def async_residents_locked(self) -> dict[str, float]:
        """Fresh current evidence, not restore targets; caller holds guard.lock."""
        residents: dict[str, float] = {}
        async with self.coordinator.native.lock:
            for inverter in self.coordinator.native.inverters.values():
                value = await self._read(self.coordinator.native.transport(inverter), inverter)
                if value is not None:
                    residents[inverter.id] = value / 10
        return residents

    async def async_guard_locked(self, requests: dict[str, float], *, epoch: int,
                                 lower_only: bool = False,
                                 cut_only: set[str] | None = None) -> IntentResult:
        """Clamp all connection peers, reductions first; caller holds guard.lock.

        Replay and request-less protection can only lower the fresh resident.
        Requests here are ephemeral and never become future restore targets.
        """
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        mismatches: set[str] = set()
        results: list[IntentResult] = []
        try:
            async with self.coordinator.native.lock:
                planned: list[tuple[Inverter, float, float]] = []
                banks = {b.id: b for b in guard.topology.banks}
                connections = {banks[i.bank_id].connection_id for i in guard.topology.inverters
                               if i.id in requests}
                if any(i.id not in requests for i in guard.topology.inverters
                       if banks[i.bank_id].connection_id in connections):
                    # A failed resident read must not make a peer disappear
                    # from the allocation and thereby fund an increase.
                    return IntentResult(outcome="failed")
                for inverter in guard.topology.inverters:
                    if inverter.id not in requests:
                        continue
                    reason = self._refusal(inverter, epoch, guard_write=True)
                    if reason is not None:
                        return IntentResult(outcome="refused", reason=reason)
                    resident = await self._read(self.coordinator.native.transport(inverter),
                                                inverter)
                    if resident is None:
                        return IntentResult(outcome="failed")
                    intent = clamp_intent(
                        DeviceIntent(inverter.id, inverter.bank_id, requests[inverter.id]),
                        allowance_kw=guard.allowance_for(inverter.bank_id),
                        last_requested_a=requests, topology=guard.topology, limits=guard.limits)
                    if intent is None:
                        return IntentResult(outcome="refused", reason="limits")
                    only_lower = lower_only or inverter.id in (cut_only or set())
                    target = min(intent.charge_a, resident / 10) if only_lower else intent.charge_a
                    planned.append((inverter, target, resident / 10))
                planned.sort(key=lambda item: item[1] >= item[2])
                for inverter, target, _ in planned:
                    result = await self._async_commit(inverter, target, epoch, mismatches,
                        guard_write=True,
                        lower_only=lower_only or inverter.id in (cut_only or set()))
                    results.append(result)
                    if result.outcome not in ("applied", "unchanged"):
                        break
        finally:
            for inverter_id in mismatches:
                await self.coordinator.native.async_readback_mismatch(inverter_id)
        return max(results, key=lambda r: _SEVERITY.index(r.outcome)) if results else (
            IntentResult(outcome="unchanged"))

    async def async_current(self, request: ChargeCurrentLimit | ChargeWindow | ExportNone,
                            *, epoch: int) -> IntentResult:
        guard = self.coordinator.guard
        if guard is None:
            return IntentResult(outcome="refused", reason="limits")
        mismatches: set[str] = set()
        try:
            return await self._async_locked(guard, request, epoch, mismatches)
        finally:
            # Re-probe takes the native lock itself: never invoke it under that
            # lock. Mismatches seen before an exception still get their probe.
            for inverter_id in mismatches:
                await self.coordinator.native.async_readback_mismatch(inverter_id)

    async def _async_locked(self, guard: "SupplyGuardLoop",
                            request: ChargeCurrentLimit | ChargeWindow | ExportNone,
                            epoch: int, mismatches: set[str]) -> IntentResult:
        # Plan entry takes both locks; the guard entry already holds guard.lock.
        async with guard.lock:
            async with self.coordinator.native.lock:
                return await self._async_held(guard, request, epoch, mismatches)

    async def _async_held(self, guard: "SupplyGuardLoop",
                          request: ChargeCurrentLimit | ChargeWindow | ExportNone,
                          epoch: int, mismatches: set[str], *,
                          slots: "NativeSlots | None" = None) -> IntentResult:
        """The whole intent; the caller holds guard.lock then native.lock."""
        inverter = next((i for i in guard.topology.inverters
                         if i.id == request.inverter_id), None)
        if inverter is None:
            return IntentResult(outcome="refused", reason="limits")
        reason = self._refusal(inverter, epoch)
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        from .native_slots import NativeSlots

        if slots is None:
            slots = NativeSlots(self, inverter, epoch, mismatches)
        if isinstance(request, ExportNone):
            return await slots.clear_discharge()
        previous = guard.request_current(inverter.id, request.current_a)
        try:
            if isinstance(request, ChargeWindow):
                # Judged on the current alone: a later slot refusal must not
                # uncount a current already written.
                result, keep = await slots.window(guard, request)
            else:
                result, attempted = await self._async_transaction(
                    guard, inverter, request, epoch, mismatches)
                keep = attempted and result.outcome != "refused"
        except BaseException:
            self._restore(guard, inverter.id, previous)
            raise
        if not keep:
            # A request refused or never attempted must not become a latent
            # peer request for a later command. Failed and unverified ones
            # stay counted: their resident current is unknown.
            self._restore(guard, inverter.id, previous)
        return result

    @staticmethod
    def _restore(guard: "SupplyGuardLoop", inverter_id: str, previous: float | None) -> None:
        if previous is None:
            guard.last_requested_a.pop(inverter_id, None)
        else:
            guard.last_requested_a[inverter_id] = previous

    async def _async_transaction(self, guard: "SupplyGuardLoop", inverter: Inverter,
                                 request: ChargeCurrentLimit, epoch: int,
                                 mismatches: set[str], *,
                                 commit: Callable[[Inverter, float], Awaitable[IntentResult]] | None
                                 = None) -> tuple[IntentResult, bool]:
        """The requested device's result, and whether its write was attempted."""
        banks = {b.id: b for b in guard.topology.banks}
        connection = banks[inverter.bank_id].connection_id
        peers = [i for i in guard.topology.inverters
                 if i.id in guard.last_requested_a and i.id != inverter.id
                 and banks[i.bank_id].connection_id == connection]
        planned: list[tuple[Inverter, float]] = []
        for device in (*peers, inverter):
            clamped = clamp_intent(
                DeviceIntent(device.id, device.bank_id,
                             guard.last_requested_a.get(device.id, 0)),
                allowance_kw=guard.allowance_for(device.bank_id),
                last_requested_a=guard.last_requested_a,
                topology=guard.topology, limits=guard.limits,
            )
            if clamped is None:
                return IntentResult(outcome="refused", reason="limits"), False
            planned.append((device, clamped.charge_a))
        # Use live currents when shares move, including a peer whose last
        # write failed verification. Unknown residents cannot fund an increase.
        residents = dict(self.written_a)
        if peers:
            for device, _ in planned:
                resident = await self._read(
                    self.coordinator.native.transport(device), device,
                )
                if resident is None:
                    self.written_a.pop(device.id, None)
                    return IntentResult(outcome="failed"), False
                residents[device.id] = resident / 10
        planned.sort(key=lambda item: item[1] >= residents.get(item[0].id, 0))
        requested_result: IntentResult | None = None
        for device, current in planned:
            result = (await commit(device, current) if commit is not None else
                      await self._async_commit(device, current, epoch, mismatches))
            if device.id == request.inverter_id:
                requested_result = result
            # A failed peer reduction must never be followed by an increase.
            if result.outcome not in ("applied", "unchanged"):
                break
        if requested_result is None:
            # A failed peer stopped the transaction before this device:
            # report the peer's outcome without attributing its written current.
            return IntentResult(outcome=result.outcome, reason=result.reason), False
        if result.outcome in ("applied", "unchanged"):
            return requested_result, True
        # A later peer failed: its outcome, never its current.
        return IntentResult(outcome=result.outcome, reason=result.reason,
                            written_a=requested_result.written_a), True

    @staticmethod
    async def _read(transport: Transport, inverter: Inverter, *, input_: bool = False
                    ) -> int | None:
        read = transport.read_input if input_ else transport.read_holding
        result = await read(33132 if input_ else 43141, 1,
                            device_id=inverter.gateway_unit_id)
        if result.error is not None or len(result.registers) != 1 or not valid_words(
                result.registers):
            return None
        return result.registers[0]

    async def _async_commit(self, inverter: Inverter, current: float, epoch: int,
                            mismatches: set[str], *, soc_required: bool = False,
                            soc_exempt: bool = False,
                            guard_write: bool = False, lower_only: bool = False,
                            fallback: bool = False, transition: bool = False,
                            entity_control: str | None = None) -> IntentResult:
        transport = self.coordinator.native.transport(inverter)
        reason = self._refusal(inverter, epoch, guard_write=guard_write,
                               fallback=fallback, transition=transition,
                               entity_control=entity_control)
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        permission = await self._read(transport, inverter, input_=True)
        if permission is None or not permission & 32:
            return IntentResult(outcome="refused", reason="grid_charge_not_permitted")
        # The resident read is the last await before the increase decision and
        # the write, so "increase" is always judged against a fresh value.
        resident = await self._read(transport, inverter)
        if resident is None:
            self.written_a.pop(inverter.id, None)
            return IntentResult(outcome="failed")
        if lower_only:
            current = min(current, resident / 10)
        target = current_word(current)
        def untrusted() -> bool:
            soc = getattr(self.coordinator, "soc", None)
            return (not soc_exempt and (target > resident or soc_required)
                    and (soc is None or not soc.increases_allowed(inverter.bank_id)))

        if untrusted():
            return IntentResult(outcome="refused", reason="untrusted_soc")

        async def read() -> list[int] | None:
            value = await self._read(transport, inverter)
            return [value] if value is not None else None

        def mismatch() -> None:
            mismatches.add(inverter.id)

        changed = resident != target
        if not changed and target == 0:
            # A transient zero must not skip the correcting write.
            changed = not await confirm([target], read, verify_sleep, changed=False,
                                        mismatch=mismatch)
            if not changed:
                self.written_a[inverter.id] = current
                self.written_at[inverter.id] = dt_util.utcnow()
                return IntentResult(outcome="unchanged", written_a=current)
        if changed:
            # Every await above may have revoked authority or changed limits.
            # No await sits between this recheck and the write.
            reason = self._refusal(inverter, epoch, guard_write=guard_write,
                                   fallback=fallback, transition=transition,
                                   entity_control=entity_control)
            if reason is not None:
                return IntentResult(outcome="refused", reason=reason)
            if untrusted():
                return IntentResult(outcome="refused", reason="untrusted_soc")
            self.written_a.pop(inverter.id, None)
            result = await transport.write(43141, [target], device_id=inverter.gateway_unit_id)
            if not result.ok:
                return IntentResult(outcome="failed")
        verified = await confirm([target], read, verify_sleep, changed=changed, mismatch=mismatch)
        if not verified:
            self.written_a.pop(inverter.id, None)
            mismatches.add(inverter.id)
            return IntentResult(outcome="unverified", written_a=current if changed else None)
        self.written_a[inverter.id] = current
        self.written_at[inverter.id] = dt_util.utcnow()
        return IntentResult(outcome="applied" if changed else "unchanged", written_a=current)
