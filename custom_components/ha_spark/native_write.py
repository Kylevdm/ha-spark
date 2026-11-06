"""One serialised, clamped and verified native charge-current boundary.

Lock order is guard then native bus. No caller-supplied SoC exemption exists.
Plan current limits, charge windows and guard ceilings share this boundary.
The guard policy permits only current changes, never window activation.
Resident state is evidence only after exact read-back, never a requested value.
Export transactions live in NativeExportWrites and share these checks and locks.
"""
import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from math import floor
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util
from pydantic import ValidationError

from .authority_store import EventIdentity
from .clamp import DeviceIntent, clamp_intent
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
    event_from_raw,
    fixed_export_refusal,
)
from .native_export_write import NativeExportWrites
from .native_verify import confirm
from .solis_frames import Reading
from .solis_transport import Transport, valid_words
from .topology import Inverter, effective_limits, validate_topology

CHARGE_CURRENT_REG = 43141
GRID_CHARGE_PERMITTED_REG = 33132
CLOCK_REG = 33022
MAX_CURRENT_TENTHS = 625

CHARGE_BLOCK_FRAME = f"holding_{CHARGE_CURRENT_REG}"
CLOCK_FRAME = f"input_{CLOCK_REG}"

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .guard_loop import SupplyGuardLoop
    from .native_slots import NativeSlots

verify_sleep = asyncio.sleep
_SEVERITY = ("applied", "unchanged", "deferred", "refused", "unverified", "failed")


def current_word(current: float) -> int:
    """Tenths of an amp, floored so a write is never above the clamped request."""
    return floor(round(max(current, 0.0) * 10, 6))


class NativeChargeBoundary(NativeExportWrites):
    """Shared guard/bus transactions with caller-specific authority policies."""

    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.written_a: dict[str, float] = {}
        self.written_at: dict[str, datetime] = {}
        self.verified_windows: dict[str, tuple[int, tuple[int, ...]]] = {}
        self.verified_fallback: dict[str, tuple[int, tuple[int, ...]]] = {}
        self.last_apply: tuple[int, datetime, bool] | None = None
        # Reloaded records are live only after the startup stage re-verified them
        # against Slot 1; records saved in this session need no such re-read.
        self.reverified: set[str] = set()
        self.armed_this_session: set[str] = set()

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
                 transition: bool = False,
                 cleanup: bool = False) -> WriteReason | None:
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
        elif cleanup:
            # A stale record is cleaned up in any owned non-Observe mode; Observe
            # is the safe exit and never writes. Only a discharge-half zero is
            # ever written on this path.
            if state.committed_mode == "observe":
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
        if (entity_control is None and not (guard_write or fallback or cleanup or transition)
                and not coordinator.control.permits_non_protection(dt_util.utcnow())):
            # Plan writes wait out the startup grace; only protection writes run in it.
            return "not_available"
        # Unloading fences new writes the moment it starts, before native closes.
        if (not coordinator._running or not coordinator.native.polling
                or coordinator.native.closed):
            return "read_only"
        reason = coordinator.native.write_reason(inverter)
        if reason is not None:
            return reason
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
        connection_epoch = transport.connection_epoch
        read = await transport.read_holding(
            CHARGE_CURRENT_REG, 30, device_id=inverter.gateway_unit_id)
        words = read.registers
        if (read.error is not None or len(words) != 30 or not valid_words(words)
                or words[0] > MAX_CURRENT_TENTHS or connection_epoch != transport.connection_epoch):
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
        c.native.readings[inverter.id][CHARGE_BLOCK_FRAME] = Reading(
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
                    connection_epoch = transport.connection_epoch
                    read = await transport.read_holding(
                        CHARGE_CURRENT_REG, 30, device_id=inverter.gateway_unit_id)
                    words = read.registers
                    if (read.error is not None or len(words) != 30
                            or not valid_words(words) or words[0] > MAX_CURRENT_TENTHS
                            or connection_epoch != transport.connection_epoch):
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
        result = await read(
            GRID_CHARGE_PERMITTED_REG if input_ else CHARGE_CURRENT_REG, 1,
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
        # Guard cuts too: with 33132 bit 5 clear there is no grid charging to cut,
        # and the guard retries on its next evaluation.
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
            result = await transport.write(
                CHARGE_CURRENT_REG, [target], device_id=inverter.gateway_unit_id)
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
