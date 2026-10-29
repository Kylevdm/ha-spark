"""Final charge boundary for fake writers; no native hardware writer exists yet.

Allowance watts / bank nominal volts follows the daemon guard's AC-equivalent
conversion, without an efficiency term. Requests share the connection budget by
power, then each bank's budget in proportion to its requested inverter currents.
The same lock must cover guard changes and clamp plus write; a queued apply can
never undo an already accepted cut. This protocol is not command authentication.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite
from typing import Protocol

from .topology import EffectiveLimits, Topology, charge_setpoint_a


@dataclass(frozen=True)
class DeviceIntent:
    """A charge-rate ceiling only, not a charge window or start command."""

    inverter_id: str
    bank_id: str
    charge_a: float


class ChargeWriter(Protocol):
    """Phase 8 supplies a verified native implementation behind this boundary."""

    async def async_write(self, intent: DeviceIntent) -> None:
        """Write a charge-rate ceiling, never start charging."""
        ...


def _current(value: float) -> float:
    return value if isfinite(value) and value >= 0 else 0


def clamp_intent(
    intent: DeviceIntent, *, allowance_kw: float | None,
    last_requested_a: Mapping[str, float], topology: Topology, limits: EffectiveLimits,
) -> DeviceIntent | None:
    """Narrow a request to its proportional share and round down; None is no command."""
    inverter = next((i for i in topology.inverters if i.id == intent.inverter_id), None)
    bank = next((b for b in topology.banks if b.id == intent.bank_id), None)
    limit = next((i for i in limits.inverters if i.id == intent.inverter_id), None)
    bank_limit = next((b for b in limits.banks if b.id == intent.bank_id), None)
    if inverter is None or bank is None or limit is None or bank_limit is None:
        return None
    if inverter.bank_id != bank.id:
        return None
    requested = _current(intent.charge_a)
    own = _current(last_requested_a.get(inverter.id, 0))
    bank_request = sum(_current(last_requested_a.get(i.id, 0))
                       for i in topology.inverters if i.bank_id == bank.id)
    bank_share = own / bank_request if bank_request > 0 else 0
    ceiling = bank_limit.charge_a * bank_share
    if allowance_kw is not None:
        allowance = _current(allowance_kw)
        power = {
            b.id: sum(_current(last_requested_a.get(i.id, 0))
                      for i in topology.inverters if i.bank_id == b.id) * b.nominal_voltage_v
            for b in topology.banks if b.connection_id == bank.connection_id
        }
        total = sum(power.values())
        share_kw = allowance * power[bank.id] / total if total > 0 else 0
        ceiling = min(ceiling, share_kw * 1000 / bank.nominal_voltage_v * bank_share)
    current = charge_setpoint_a(min(requested, own, ceiling), inverter, limit)
    return replace(intent, charge_a=current) if current is not None else None


class ClampGuard(Protocol):
    """Shared boundary state, changed only under lock."""

    lock: asyncio.Lock
    topology: Topology
    limits: EffectiveLimits
    last_requested_a: dict[str, float]

    def allowance_for(self, bank_id: str) -> float | None:
        """Connection allowance for this bank; None means no guard cut."""
        ...

    def permits_writes(self) -> bool:
        """False for Observe, a missing, stale or unknown mirror, or a failed guard save."""
        ...


class ClampedWriter:
    """Serialise both plan applies and guard cuts using the guard's lock."""

    def __init__(self, inner: ChargeWriter, guard: ClampGuard) -> None:
        self.inner = inner
        self.guard = guard
        # Last current written per inverter: what is resident behind the boundary.
        self.written_a: dict[str, float] = {}

    def _clamp(self, intent: DeviceIntent) -> DeviceIntent | None:
        return clamp_intent(
            intent, allowance_kw=self.guard.allowance_for(intent.bank_id),
            last_requested_a=self.guard.last_requested_a,
            topology=self.guard.topology, limits=self.guard.limits,
        )

    async def _write(self, clamped: DeviceIntent) -> None:
        await self.inner.async_write(clamped)
        self.written_a[clamped.inverter_id] = clamped.charge_a

    async def async_write(self, intent: DeviceIntent) -> None:
        """All fake writes pass the clamp while holding the guard update lock.

        A new request moves every share on its connection, so each inverter
        already written there is re-clamped too, reductions before increases:
        the resident total never exceeds the allowance between writes.
        """
        async with self.guard.lock:
            self.guard.last_requested_a[intent.inverter_id] = _current(intent.charge_a)
            if not self.guard.permits_writes():
                return
            banks = {b.id: b for b in self.guard.topology.banks}
            target = banks.get(intent.bank_id)
            peers = [
                DeviceIntent(i.id, i.bank_id, self.guard.last_requested_a.get(i.id, 0))
                for i in self.guard.topology.inverters
                if target is not None and i.id != intent.inverter_id and i.id in self.written_a
                and i.bank_id in banks
                and banks[i.bank_id].connection_id == target.connection_id
            ]
            planned = [c for c in (self._clamp(p) for p in (*peers, intent)) if c is not None]
            planned.sort(key=lambda c: c.charge_a >= self.written_a.get(c.inverter_id, 0))
            for clamped in planned:
                if (clamped.inverter_id == intent.inverter_id
                        or clamped.charge_a != self.written_a.get(clamped.inverter_id)):
                    await self._write(clamped)

    async def async_write_locked(self, intent: DeviceIntent) -> None:
        """Guard re-apply with an unchanged request, under the already-held lock."""
        if not self.guard.permits_writes():
            return
        clamped = self._clamp(intent)
        if clamped is not None:
            await self._write(clamped)
