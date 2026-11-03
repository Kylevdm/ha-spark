"""#578's gated adapter onto #580's 43007 power-switch helper.

This module never touches the transport itself: `native_power.async_write_power_locked`
does the pre-read, write and read-back, and the entity policy in `_refusal` is its
recheck (no await between it and the write, inside the helper).
"""
from typing import TYPE_CHECKING, cast, get_args

from . import native_power
from .commands import IntentResult, WriteReason

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator


async def async_set_power_switch(coordinator: "SparkCoordinator", inverter_id: str,
                                 on: bool, *, epoch: int) -> IntentResult:
    native = coordinator.native
    boundary = coordinator.writes
    inverter = native.inverters.get(inverter_id)
    if inverter is None or boundary is None:
        return IntentResult(outcome="refused", reason="not_available")

    def recheck() -> WriteReason | None:
        return boundary._refusal(inverter, epoch, entity_control="power")

    async with native.lock:
        reason = recheck()
        if reason is not None:
            return IntentResult(outcome="refused", reason=reason)
        write = await native_power.async_write_power_locked(
            native, inverter, "on" if on else "off", recheck=recheck)
    if write.mismatch:
        await native.async_readback_mismatch(inverter.id)
    if write.outcome == "refused":
        refused = (cast(WriteReason, write.reason) if write.reason in get_args(WriteReason)
                   else "not_available")
        return IntentResult(outcome="refused", reason=refused)
    if write.outcome == "unreadable":
        # "On" only to a readable switch: no write happened.
        return IntentResult(outcome="refused", reason="read_only")
    return IntentResult(outcome=write.outcome)
