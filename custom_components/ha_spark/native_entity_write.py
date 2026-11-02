"""Owner operations inside NativeChargeBoundary, never a second charge writer.

Both locks and the entity policy cover the whole transaction. Time edits write
only the whole Slot 1 block (ruling Q9a); discharge edits are manual-only. The power
switch deliberately belongs to the separate #580 adapter, not this module.
"""
from dataclasses import replace
from math import isfinite
from typing import TYPE_CHECKING

from . import native_write
from .commands import ChargeCurrentLimit, IntentResult
from .native_slots import NativeSlots
from .native_verify import confirm
from .solis_transport import valid_words
from .topology import Inverter

if TYPE_CHECKING:
    from .native_write import NativeChargeBoundary


async def async_entity(boundary: "NativeChargeBoundary", inverter_id: str, control: str,
                       value: float | list[int], *, epoch: int,
                       offset: int | None) -> IntentResult:
    coordinator = boundary.coordinator
    inverter = coordinator.native.inverters.get(inverter_id)
    guard = coordinator.guard
    if inverter is None or guard is None or control not in ("charge", "discharge"):
        return IntentResult(outcome="refused", reason="limits")
    mismatches: set[str] = set()
    try:
        async with guard.lock:
            async with coordinator.native.lock:
                reason = boundary._refusal(inverter, epoch, entity_control=control)
                if reason:
                    return IntentResult(outcome="refused", reason=reason)
                if offset is None and isinstance(value, (int, float)):
                    if not isfinite(value) or value < 0:
                        return IntentResult(outcome="refused", reason="invalid_intent")
                    if control == "discharge":
                        limit = next(i.discharge_a for i in guard.limits.inverters
                                     if i.id == inverter.id)
                        return await words(boundary, inverter, epoch, control, 43142,
                                           [int(min(value, limit, 62.5)) * 10], mismatches)
                    current = min(value, 62.5)
                elif (offset in (0, 2, 4, 6) and isinstance(value, list)
                      and len(value) == 2 and valid_words(value)
                      and value[0] < 24 and value[1] < 60
                      and (offset < 4) == (control == "charge")):
                    current = 0.0
                else:
                    return IntentResult(outcome="refused", reason="invalid_intent")

                slots = NativeSlots(boundary, inverter, epoch, mismatches)
                target: list[int] | None = None
                if offset is not None:
                    target = await slots.read(43143)
                    if target is None:
                        return IntentResult(outcome="failed")
                    assert isinstance(value, list)
                    target[offset:offset + 2] = value
                    half = target[:4] if offset < 4 else target[4:]
                    if (any(h >= 24 or m >= 60 for h, m in zip(target[::2], target[1::2],
                                                                             strict=True))
                            or half[:2] == half[2:] and any(half)):
                        return IntentResult(outcome="refused", reason="invalid_intent")
                    if control == "charge" and slots.soc_untrusted():
                        return IntentResult(outcome="refused", reason="untrusted_soc")

                if control == "charge" and offset is None:
                    # Q9(a): a time edit writes only the Slot 1 block, never 43141.
                    previous = guard.request_current(inverter.id, current)
                    async def commit(device: Inverter, amps: float) -> IntentResult:
                        result = await boundary._async_commit(device, amps, epoch, mismatches,
                                                             entity_control="charge")
                        if result.outcome in ("applied", "unchanged"):
                            publish(boundary, device, 43141, [int(amps) * 10])
                        return result
                    try:
                        result, attempted = await boundary._async_transaction(
                            guard, inverter, ChargeCurrentLimit(kind="charge_current_limit",
                                inverter_id=inverter.id, current_a=current), epoch, mismatches,
                            commit=commit)
                    except BaseException:
                        boundary._restore(guard, inverter.id, previous)
                        raise
                    if not attempted or result.outcome == "refused":
                        boundary._restore(guard, inverter.id, previous)
                    if result.outcome not in ("applied", "unchanged") or target is None:
                        return result
                assert target is not None
                return await words(boundary, inverter, epoch, control, 43143, target, mismatches)
    finally:
        for id_ in mismatches:
            await coordinator.native.async_readback_mismatch(id_)


async def words(boundary: "NativeChargeBoundary", inverter: Inverter, epoch: int,
                control: str, address: int, target: list[int], mismatches: set[str]
                ) -> IntentResult:
    """Whole-block write and exact confirmation, under the boundary's locks."""
    transport = boundary.coordinator.native.transport(inverter)
    async def read() -> list[int] | None:
        result = await transport.read_holding(address, len(target),
                                               device_id=inverter.gateway_unit_id)
        return (list(result.registers) if result.error is None
                and len(result.registers) == len(target) and valid_words(result.registers)
                else None)

    def mismatch() -> None:
        mismatches.add(inverter.id)

    if control == "charge":
        permission = await boundary._read(transport, inverter, input_=True)
        if permission is None or not permission & 32:
            return IntentResult(outcome="refused", reason="grid_charge_not_permitted")
    resident = await read()
    if resident is None:
        return IntentResult(outcome="failed")
    changed = resident != target
    if not changed:
        if await confirm(target, read, native_write.verify_sleep, changed=False,
                         mismatch=mismatch):
            publish(boundary, inverter, address, target)
            return IntentResult(outcome="unchanged")
        if any(target):
            return IntentResult(outcome="unverified")
    reason = boundary._refusal(inverter, epoch, entity_control=control)
    if reason:
        return IntentResult(outcome="refused", reason=reason)
    if control == "charge" and not boundary.coordinator.soc.increases_allowed(inverter.bank_id):
        return IntentResult(outcome="refused", reason="untrusted_soc")
    result = await transport.write(address, target, device_id=inverter.gateway_unit_id)
    if not result.ok:
        return IntentResult(outcome="failed")
    verified = await confirm(target, read, native_write.verify_sleep, changed=True,
                             mismatch=mismatch)
    if verified:
        publish(boundary, inverter, address, target)
    return IntentResult(outcome="applied" if verified else "unverified")


def publish(boundary: "NativeChargeBoundary", inverter: Inverter,
            address: int, target: list[int]) -> None:
    """Update verified words without refreshing the age of untouched frame words."""
    native = boundary.coordinator.native
    reading = native.reading(inverter, "holding_43141")
    if reading is not None and reading.registers is not None:
        registers = list(reading.registers)
        start = address - 43141
        registers[start:start + len(target)] = target
        native.readings[inverter.id]["holding_43141"] = replace(
            reading, registers=tuple(registers))
