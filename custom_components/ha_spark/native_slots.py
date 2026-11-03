"""Solis Slot 1 commit semantics inside the shared charge-write transaction.

The caller holds guard.lock then native.lock throughout. Slot blocks are always
fresh-read and written whole. Export arming is not enabled here; planned_discharge
is the parity seam for keeping an exact resident export during a current change.
"""
from collections.abc import Callable
from typing import TYPE_CHECKING

from . import native_write
from .commands import ChargeCurrentLimit, ChargeWindow, IntentResult, WriteReason
from .native_verify import confirm
from .solis_transport import valid_words
from .topology import Inverter

if TYPE_CHECKING:
    from .guard_loop import SupplyGuardLoop
    from .native_write import NativeChargeBoundary


def worst(results: list[IntentResult], *, written_a: float | None = None) -> IntentResult:
    result = max(results, key=lambda r: native_write._SEVERITY.index(r.outcome))
    return result.model_copy(update={"written_a": written_a})


class NativeSlots:
    """Register operations requiring the boundary's two locks, never a new writer."""

    def __init__(self, boundary: "NativeChargeBoundary", inverter: Inverter,
                 epoch: int, mismatches: set[str], *,
                 planned_discharge: list[int] | None = None,
                 fallback: bool = False, transition: bool = False,
                 cleanup: bool = False) -> None:
        self.boundary = boundary
        self.inverter = inverter
        self.epoch = epoch
        self.mismatches = mismatches
        self.planned_discharge = planned_discharge or [0] * 4
        self.fallback = fallback
        self.transition = transition
        self.cleanup = cleanup
        self.block_result: IntentResult | None = None
        # Set once a Slot 1 block whose discharge half is zero carries the five
        # further matching reads, so a later retire never confirms it twice.
        self.zero_half_confirmed = False
        # Export permission, rechecked before any write carrying a discharge half;
        # without one such a write is refused.
        self.discharge_check: Callable[[], WriteReason | None] | None = None
        # A charge window needs trusted SoC for every write, whatever its direction.
        self.soc_required = False

    def soc_untrusted(self) -> bool:
        soc = getattr(self.boundary.coordinator, "soc", None)
        return soc is None or not soc.increases_allowed(self.inverter.bank_id)

    def mismatch(self) -> None:
        self.mismatches.add(self.inverter.id)

    async def read(self, address: int) -> list[int] | None:
        result = await self.boundary.coordinator.native.transport(self.inverter).read_holding(
            address, 8, device_id=self.inverter.gateway_unit_id)
        if result.error is not None or len(result.registers) != 8 or not valid_words(
                result.registers):
            return None
        return list(result.registers)

    async def block(self, address: int, target: list[int]) -> IntentResult:
        """Write-if-changed; a transient all-zero pre-read never skips a write."""
        resident = await self.read(address)
        if resident is None:
            return IntentResult(outcome="failed")

        # Every discharge-half clear carries the shipped sustained-zero rule.
        sustained = address == 43143 and not any(target[4:])

        async def read() -> list[int] | None:
            return await self.read(address)

        if resident == target:
            if await confirm(target, read, native_write.verify_sleep,
                             changed=False, mismatch=self.mismatch, sustained=sustained):
                if address == 43143:
                    self.zero_half_confirmed = sustained
                return IntentResult(outcome="unchanged")
            if any(target):
                return IntentResult(outcome="unverified")
        if self.soc_required and self.soc_untrusted():
            return IntentResult(outcome="refused", reason="untrusted_soc")
        reason = self.boundary._refusal(self.inverter, self.epoch,
                                        fallback=self.fallback,
                                        transition=self.transition,
                                        cleanup=self.cleanup)
        if reason is None and address == 43143 and any(target[4:]):
            reason = self.discharge_check() if self.discharge_check is not None else "mode"
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        result = await self.boundary.coordinator.native.transport(self.inverter).write(
            address, target, device_id=self.inverter.gateway_unit_id)
        if not result.ok:
            return IntentResult(outcome="failed")
        verified = await confirm(target, read, native_write.verify_sleep,
                                 changed=True, mismatch=self.mismatch, sustained=sustained)
        if address == 43143:
            self.zero_half_confirmed = verified and sustained
        return IntentResult(outcome="applied" if verified else "unverified")

    async def clear_discharge(self) -> IntentResult:
        """Zero the discharge half, keeping the charge half from a trusted read."""
        resident = await self.read(43143)
        if resident is None:
            return IntentResult(outcome="failed")
        if not any(resident):
            # A transient zero must not become the charge half written back.
            async def read() -> list[int] | None:
                return await self.read(43143)
            if await confirm([0] * 8, read, native_write.verify_sleep,
                             changed=False, mismatch=self.mismatch):
                self.zero_half_confirmed = True
                return IntentResult(outcome="unchanged")
            resident = await self.read(43143)
            if resident is None:
                return IntentResult(outcome="failed")
        return await self.block(43143, resident[:4] + [0] * 4)

    async def arm(self, discharge: list[int], *, zero_charge: bool = False) -> IntentResult:
        """Replace Slot 1's discharge half, keeping the resident charge half."""
        resident = await self.read(43143)
        if resident is None:
            return IntentResult(outcome="failed")
        if zero_charge:
            return await self.block(43143, [0] * 4 + discharge)
        if not any(resident):
            async def read() -> list[int] | None:
                return await self.read(43143)
            if not await confirm([0] * 8, read, native_write.verify_sleep,
                                 changed=False, mismatch=self.mismatch):
                resident = await self.read(43143)
                if resident is None:
                    return IntentResult(outcome="failed")
        return await self.block(43143, resident[:4] + discharge)

    async def prepare(self, current: float) -> IntentResult:
        resident = await self.read(43143)
        if resident is None:
            return IntentResult(outcome="failed")
        active = any(resident)
        if not active:
            async def read() -> list[int] | None:
                return await self.read(43143)
            active = not await confirm([0] * 8, read, native_write.verify_sleep,
                                       changed=False, mismatch=self.mismatch)
        if active:
            transport = self.boundary.coordinator.native.transport(self.inverter)
            amps = await self.boundary._read(transport, self.inverter)
            if amps is None or abs(amps / 10 - current) > 0.05:
                keep = (self.planned_discharge if any(self.planned_discharge)
                        and resident[4:] == self.planned_discharge else [0] * 4)
                return await self.block(43143, [0] * 4 + keep)
        return IntentResult(outcome="unchanged")

    async def window(self, guard: "SupplyGuardLoop", request: ChargeWindow,
                     *, soc_required: bool = True
                     ) -> tuple[IntentResult, bool]:
        if request.start == request.end:
            return IntentResult(outcome="refused", reason="invalid_intent"), False
        self.soc_required = soc_required
        if soc_required and self.soc_untrusted():
            return IntentResult(outcome="refused", reason="untrusted_soc"), False
        transport = self.boundary.coordinator.native.transport(self.inverter)
        permission = await self.boundary._read(transport, self.inverter, input_=True)
        results: list[IntentResult] = []
        current_result = IntentResult(outcome="refused", reason="grid_charge_not_permitted")
        attempted = False
        if permission is not None and permission & 32:
            async def commit(device: Inverter, current: float) -> IntentResult:
                nonlocal attempted
                if device.id != self.inverter.id:
                    # A peer rebalance is a plain current change (#576): its own
                    # Slot 1 schedule is never zeroed by another inverter's window.
                    return await self.boundary._async_commit(
                        device, current, self.epoch, self.mismatches,
                        fallback=self.fallback, transition=self.transition)
                device_transport = self.boundary.coordinator.native.transport(device)
                allowed = await self.boundary._read(device_transport, device, input_=True)
                if allowed is None or not allowed & 32:
                    return IntentResult(outcome="refused", reason="grid_charge_not_permitted")
                preparation = await self.prepare(current)
                results.append(preparation)
                if preparation.outcome not in ("applied", "unchanged"):
                    return preparation
                attempted = True
                return await self.boundary._async_commit(
                    device, current, self.epoch, self.mismatches,
                    soc_required=self.soc_required, soc_exempt=self.fallback,
                    fallback=self.fallback, transition=self.transition)

            current_result, _ = await self.boundary._async_transaction(
                guard, self.inverter, ChargeCurrentLimit(kind="charge_current_limit",
                    inverter_id=request.inverter_id, current_a=request.current_a),
                self.epoch, self.mismatches, commit=commit)
        results.append(current_result)
        if current_result.outcome in ("applied", "unchanged"):
            charge = [int(part) for time in (request.start, request.end)
                      for part in time.split(":")]
            self.block_result = await self.block(43143, charge + self.planned_discharge)
            results.append(self.block_result)
        elif any(self.planned_discharge):
            # Permission gates charging only: a ready export still replaces the
            # discharge half, keeping the resident charge half (or zeroing it
            # when the requested charge is zero).
            self.block_result = await self.arm(
                self.planned_discharge, zero_charge=request.current_a <= 0)
            results.append(self.block_result)
        else:
            self.block_result = await self.clear_discharge()
            results.append(self.block_result)
        for address in (43153, 43163):
            results.append(await self.block(address, [0] * 8))
        if all(r.outcome in ("applied", "unchanged") for r in results):
            block = tuple(charge + self.planned_discharge)
            if self.fallback:
                # The verified fallback current is part of the schedule restored to.
                written = current_result.written_a
                if written is not None:
                    self.boundary.verified_fallback[self.inverter.id] = (
                        self.epoch, (round(written * 10), *block))
            else:
                self.boundary.verified_windows[self.inverter.id] = (self.epoch, block)
        # Whether the current stays counted is judged on the current alone.
        return (worst(results, written_a=current_result.written_a),
                attempted and current_result.outcome != "refused")
