"""Shared 43007 codec and verified edge I/O; callers own authority and locking.

An Off may close an unreadable switch; an On always needs a readable pre-read.
The caller must probe a mismatch after releasing the native bus lock.
"""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from .solis_frames import Reading
from .solis_transport import valid_words

if TYPE_CHECKING:
    from .native import NativeDevices
    from .topology import Inverter

POWER_SWITCH_REG = 43007
POWER_ON = 190
POWER_OFF = 222
PowerState = Literal["on", "off"]
PowerOutcome = Literal["unchanged", "applied", "unverified", "failed", "unreadable", "refused"]
verify_sleep = asyncio.sleep


def decode_power(words: list[int] | None) -> PowerState | None:
    if words is None or len(words) != 1 or not valid_words(words):
        return None
    return "on" if words == [POWER_ON] else "off" if words == [POWER_OFF] else None


def polled_power(native: "NativeDevices", inverter: "Inverter", now: datetime) -> PowerState | None:
    reading = native.reading(inverter, "holding_43007")
    if reading is None or not timedelta(0) <= now - reading.read_at <= timedelta(seconds=60):
        return None
    return decode_power(list(reading.registers) if reading.registers is not None else None)


@dataclass(frozen=True)
class PowerWrite:
    outcome: PowerOutcome
    reason: str | None
    before: PowerState | None
    after: PowerState | None
    mismatch: bool


async def async_write_power_locked(
    native: "NativeDevices", inverter: "Inverter", target: PowerState,
    *, recheck: Callable[[], str | None],
) -> PowerWrite:
    """Caller holds native.lock; no await separates the final gate and write."""
    transport = native.transport(inverter)

    async def read() -> PowerState | None:
        try:
            epoch = transport.connection_epoch
            result = await transport.read_holding(
                POWER_SWITCH_REG, 1, device_id=inverter.gateway_unit_id)
            if result.error is not None or epoch != transport.connection_epoch:
                return None
            value = decode_power(list(result.registers))
            if value is not None:
                native.readings.setdefault(inverter.id, {})["holding_43007"] = Reading(
                    datetime.now(UTC), tuple(result.registers), epoch)
            return value
        except Exception:
            return None

    before = await read()
    if target == "on" and before is None:
        return PowerWrite("unreadable", None, before, None, False)
    if before == target:
        return PowerWrite("unchanged", None, before, before, False)
    reason = recheck()
    if reason is not None:
        return PowerWrite("refused", reason, before, None, False)
    try:
        result = await transport.write(
            POWER_SWITCH_REG, [POWER_ON if target == "on" else POWER_OFF],
            device_id=inverter.gateway_unit_id)
        if not result.ok:
            return PowerWrite("failed", None, before, None, False)
    except Exception:
        return PowerWrite("failed", None, before, None, False)
    after = None
    for attempt in range(3):
        if attempt:
            await verify_sleep(1)
        after = await read()
        if after == target:
            return PowerWrite("applied", None, before, after, False)
    return PowerWrite("unverified", None, before, after, True)
