"""One serialised, clamped and verified native charge-current boundary.

Lock order is guard then native bus. No caller-supplied SoC exemption exists.
Plan current limits and charge windows are enabled here; fallback, protection and entity
callers must acquire their own authority policy before being enabled later.
Resident state is evidence only after exact read-back, never a requested value.
"""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .clamp import DeviceIntent, clamp_intent
from .commands import (
    ApplyPayload,
    ChargeCurrentLimit,
    ChargeWindow,
    Command,
    CommandResult,
    ExportNone,
    IntentResult,
    WriteReason,
)
from .native_verify import confirm
from .solis_transport import Transport, valid_words
from .topology import Inverter, effective_limits, validate_topology

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .guard_loop import SupplyGuardLoop

verify_sleep = asyncio.sleep
_SEVERITY = ("applied", "unchanged", "deferred", "refused", "unverified", "failed")


class NativeChargeBoundary:
    """Shared guard/bus transaction, deliberately not installed as a guard writer."""

    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self.coordinator = coordinator
        self.written_a: dict[str, float] = {}

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

    def _refusal(self, inverter: Inverter, epoch: int) -> WriteReason | None:
        coordinator = self.coordinator
        state = coordinator.authority.state
        if state is None:
            return "authority_unknown"
        if not coordinator.control.owned():
            return "gateway_not_owned"
        if state.epoch != epoch:
            return "stale_epoch"
        if (state.committed_mode != "optimise" or state.manual_session is not None
                or state.fallback.active
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

    async def async_apply(self, command: Command) -> CommandResult:
        """Isolate intents and aggregate the worst positional result."""
        results: list[IntentResult] = []
        for raw in ApplyPayload.model_validate(command.payload).intents:
            schemas: dict[str, type[ChargeCurrentLimit] | type[ChargeWindow] | type[ExportNone]] = {
                "charge_current_limit": ChargeCurrentLimit, "charge_window": ChargeWindow,
                "export": ExportNone}
            kind = raw.get("kind")
            if not isinstance(kind, str) or kind not in schemas or (
                    kind == "export" and raw.get("request") != "none"):
                results.append(IntentResult(outcome="refused", reason="not_available"))
                continue
            try:
                intent = schemas[kind].model_validate(raw)
            except ValidationError:
                results.append(IntentResult(outcome="refused", reason="invalid_intent"))
                continue
            try:
                results.append(await self.async_current(intent, epoch=command.epoch))
            except Exception:
                # Fixed outcomes only; a broken device cannot skip its peers.
                self.written_a.pop(intent.inverter_id, None)
                results.append(IntentResult(outcome="failed"))
        worst = max(results, key=lambda r: _SEVERITY.index(r.outcome)) if results else (
            IntentResult(outcome="refused", reason="invalid_intent"))
        return CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome=worst.outcome, reason=worst.reason, intents=results)

    async def async_write(self, intent: DeviceIntent) -> None:
        """ChargeWriter seam; no mode expansion or production writer attachment."""
        state = self.coordinator.authority.state
        if state is not None:
            await self.async_current(ChargeCurrentLimit(
                kind="charge_current_limit", inverter_id=intent.inverter_id,
                current_a=intent.charge_a), epoch=state.epoch)

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
        # The only place the boundary takes its locks, guard then native bus.
        async with guard.lock:
            async with self.coordinator.native.lock:
                return await self._async_held(guard, request, epoch, mismatches)

    async def _async_held(self, guard: "SupplyGuardLoop",
                          request: ChargeCurrentLimit | ChargeWindow | ExportNone,
                          epoch: int, mismatches: set[str]) -> IntentResult:
        """The whole intent; the caller holds guard.lock then native.lock."""
        inverter = next((i for i in guard.topology.inverters
                         if i.id == request.inverter_id), None)
        if inverter is None:
            return IntentResult(outcome="refused", reason="limits")
        reason = self._refusal(inverter, epoch)
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        from .native_slots import NativeSlots

        slots = NativeSlots(self, inverter, epoch, mismatches)
        if isinstance(request, ExportNone):
            return await slots.clear_discharge()
        previous = guard.last_requested_a.get(inverter.id)
        guard.last_requested_a[inverter.id] = request.current_a
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
                            mismatches: set[str], *, soc_required: bool = False) -> IntentResult:
        transport = self.coordinator.native.transport(inverter)
        reason = self._refusal(inverter, epoch)
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
        target = int(current) * 10
        def untrusted() -> bool:
            soc = getattr(self.coordinator, "soc", None)
            return (target > resident or soc_required) and (
                soc is None or not soc.increases_allowed(inverter.bank_id))

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
                return IntentResult(outcome="unchanged", written_a=current)
        if changed:
            # Every await above may have revoked authority or changed limits.
            # No await sits between this recheck and the write.
            reason = self._refusal(inverter, epoch)
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
        return IntentResult(outcome="applied" if changed else "unchanged", written_a=current)
