"""Native clock I/O: authority-gated, bus-serialised and always read back.

Explicit owner sync is allowed even in Observe or manual control. Automatic
sync only handles zone-rule changes in active modes, retrying inside six hours.
Clock writes are not charge writes and do not take the supply-guard lock.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .clock_sync import (
    CLOCK_SYNC_REG,
    DRIFT_TOLERANCE,
    FAIL_AT,
    READ_BACK_TOLERANCE,
    STALE_AFTER,
    clock_error,
    clock_registers,
    last_clock_change,
)
from .commands import CommandResult
from .const import DOMAIN
from .solis_frames import Reading
from .solis_transport import valid_words
from .topology import Inverter

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator

verify_sleep = asyncio.sleep
_LOGGER = logging.getLogger(__name__)


class NativeClock:
    """One clock-change episode per inverter; no persisted write permissions."""

    def __init__(self, coordinator: SparkCoordinator) -> None:
        self.coordinator = coordinator
        self.handled: dict[str, datetime] = {}
        self.failure_logged: dict[str, datetime] = {}
        self._tick_lock = asyncio.Lock()

    def issue_id(self, inverter: Inverter) -> str:
        key = f"{self.coordinator.entry.entry_id}:{inverter.id}"
        return f"clock_drift_{uuid5(NAMESPACE_URL, key)}"

    def check_drift(self, now: datetime) -> None:
        """Only fresh readable evidence creates or clears a drift repair."""
        c = self.coordinator
        zone = c.control.household_zone()
        if zone is None:
            return
        for inverter in c.native.inverters.values():
            reading = c.native.reading(inverter, "input_33022")
            if (reading is None or reading.registers is None
                    or not timedelta(0) <= now - reading.read_at <= STALE_AFTER):
                continue
            error = clock_error(reading.registers, reading.read_at, zone)
            if error is None:
                continue
            if abs(error) <= DRIFT_TOLERANCE:
                ir.async_delete_issue(c.hass, DOMAIN, self.issue_id(inverter))
            else:
                ir.async_create_issue(
                    c.hass, DOMAIN, self.issue_id(inverter), is_fixable=True,
                    severity=(ir.IssueSeverity.ERROR if abs(error) >= FAIL_AT
                              else ir.IssueSeverity.WARNING),
                    translation_key="clock_drift",
                    data={"entry_id": c.entry.entry_id, "inverter_id": inverter.id},
                )

    def _refusal(self, inverter: Inverter, epoch: int | None, store_id: object,
                 automatic: bool) -> CommandResult | None:
        c = self.coordinator
        state = c.authority.state
        reason = None
        if state is None:
            reason = "authority_unknown"
        elif not c.control.owned():
            reason = "not_owner"
        elif state.epoch != epoch or state.store_id != store_id:
            reason = "stale_authority"
        elif state.transition is not None:
            reason = "transition_in_progress"
        elif not c._running or not c.native.polling or c.native.closed:
            reason = "not_available"
        elif c.control.household_zone() is None:
            reason = "invalid_household_zone"
        elif c.native.reason(inverter) is not None:
            reason = "read_only"
        elif automatic and (state.committed_mode not in ("protect", "optimise")
                            or state.manual_session is not None):
            reason = "mode"
        if reason is None:
            return None
        return CommandResult.model_validate({"session_id": "clock", "sequence": 1,
                                             "outcome": "refused", "reason": reason})

    async def async_sync(self, inverter_id: str, *, epoch: int | None = None,
                         automatic: bool = False) -> CommandResult:
        """One isolated read/write/verify transaction; never trusts a cached echo."""
        c = self.coordinator
        base = CommandResult(session_id="clock", sequence=1, outcome="refused",
                             reason="native_device_missing")
        inverter = c.native.inverters.get(inverter_id)
        if inverter is None:
            return base
        state = c.authority.state
        if epoch is None and state is not None:
            epoch = state.epoch
        store_id = state.store_id if state else None
        mismatch = False
        async with c.native.lock:
            refusal = self._refusal(inverter, epoch, store_id, automatic)
            if refusal is not None:
                return refusal
            transport = c.native.transport(inverter)
            unit = inverter.gateway_unit_id
            # An unreadable initial error never prevents an authorised sync.
            try:
                await transport.read_input(33022, 6, device_id=unit)
            except Exception:
                pass
            refusal = self._refusal(inverter, epoch, store_id, automatic)
            if refusal is not None:
                return refusal
            zone = c.control.household_zone()
            assert zone is not None
            try:
                written = await transport.write(
                    CLOCK_SYNC_REG, clock_registers(dt_util.utcnow(), zone), device_id=unit,
                )
                outcome = "unverified" if written.ok else "failed"
                if written.ok:
                    for attempt in range(3):
                        if attempt:
                            await verify_sleep(1)
                        try:
                            connection_epoch = getattr(transport, "connection_epoch", 0)
                            read = await transport.read_input(33022, 6, device_id=unit)
                            at = dt_util.utcnow()
                            if (read.error is not None or connection_epoch !=
                                    getattr(transport, "connection_epoch", 0)):
                                continue
                            error = clock_error(read.registers, at, zone)
                            if len(read.registers) == 6 and valid_words(read.registers):
                                # Same admission as the poll: only whole, valid frames are cached.
                                c.native.readings[inverter_id]["input_33022"] = Reading(
                                    at, read.registers, connection_epoch)
                            if error is not None and abs(error) <= READ_BACK_TOLERANCE:
                                outcome = "applied"
                                break
                        except Exception:
                            continue
            except Exception:
                outcome = "failed"
            mismatch = outcome != "applied"
            result = base.model_copy(update={"outcome": outcome, "reason": None, "epoch": epoch})
        if mismatch:
            try:
                await c.native.async_readback_mismatch(inverter_id)
            except Exception:
                # Re-probe has already invalidated its old validation evidence.
                # A failed diagnostic must not lose the write result or its peers.
                c.native.recheck()
        self.check_drift(dt_util.utcnow())
        c.async_update_listeners()
        return result

    async def async_command(self, epoch: int) -> CommandResult:
        """All native inverters in topology order, failures isolated, worst result."""
        results = [await self.async_sync(i, epoch=epoch) for i in self.coordinator.native.inverters]
        if not results:
            return CommandResult(session_id="clock", sequence=1, outcome="refused",
                                 reason="native_device_missing")
        severity = {"applied": 0, "refused": 1, "unverified": 2, "failed": 3}
        return max(results, key=lambda r: severity.get(r.outcome, 3))

    async def async_tick(self, now: datetime) -> None:
        """Retry a due zone change, never drift-triggered automatic writes."""
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            self.check_drift(now)
            zone = self.coordinator.control.household_zone()
            if zone is None:
                return
            change = last_clock_change(now.astimezone(zone))
            if change is None:
                return
            for inverter_id in self.coordinator.native.inverters:
                if self.handled.get(inverter_id) == change:
                    continue
                result = await self.async_sync(inverter_id, automatic=True)
                if result.outcome == "applied":
                    self.handled[inverter_id] = change
                    _LOGGER.info("Inverter clock synced at household clock change")
                elif (result.outcome in ("unverified", "failed")
                      and now - change >= FAIL_AT
                      and self.failure_logged.get(inverter_id) != change):
                    self.failure_logged[inverter_id] = change
                    _LOGGER.warning("Inverter clock sync unconfirmed after household clock change")
