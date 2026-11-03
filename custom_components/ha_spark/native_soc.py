"""Per-bank trust from validated native frames only; no additional bus traffic.

A failed live frame ages the last good read. Lost validation or a connection
incarnation change masks trust immediately. Collection and failure counting run
only for the confirmed integration owner with a working authority store.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .soc_trust import BankTrust, Policy, SocMonitor
from .solis_frames import POINTS, Reading, decode
from .topology import Inverter, Topology, validate_topology

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator


class NativeSoc:
    """Read-only narrowing seam for writers, fallback, preconditions and snapshots."""

    def __init__(self, coordinator: SparkCoordinator) -> None:
        self.coordinator = coordinator
        topology = validate_topology(coordinator.entry.options.get("topology"))
        self.topology = topology if isinstance(topology, Topology) else None
        try:
            self.policy: Policy | None = Policy.model_validate(dict(coordinator.entry.options))
        except ValidationError:
            self.policy = None
        self.monitors = {b.id: SocMonitor(self.policy) for b in self.topology.banks} if (
            self.topology and self.policy) else {}
        self._good: dict[str, Reading] = {}

    def _owned(self) -> bool:
        c = self.coordinator
        return (c.ownership.verified and c.ownership.owner == "integration"
                and c.authority.state is not None)

    def _source(self, bank_id: str) -> Inverter | None:
        sources = [i for i in self.coordinator.native.inverters.values() if i.bank_id == bank_id]
        return sources[0] if len(sources) == 1 else None

    def observe(self, now: datetime) -> None:
        """Minute-deduplicated observations; last-good cache is never a reconnect substitute."""
        if not self._owned() or self.topology is None or self.policy is None:
            return
        state = self.coordinator.authority.state
        assert state is not None
        fallback = state.fallback.active and "soc_integrity" in state.fallback.causes
        native = self.coordinator.native
        for bank in self.topology.banks:
            source = self._source(bank.id)
            if source is None:
                # Ruling 5: no single native source is never trusted, never counted.
                continue
            valid = native.reason(source) is None
            reading = native.reading(source, "input_33132")
            previous = self._good.get(bank.id)
            if previous is not None and previous.epoch != native.transport(source).connection_epoch:
                valid = False
            if not valid:
                self._good.pop(bank.id, None)
            elif reading is not None:
                self._good[bank.id] = reading
            good = self._good.get(bank.id)
            value = power = None
            if good is not None and good.registers is not None:
                value = decode(next(p for p in POINTS if p.address == 33139),
                               good.registers, None, frame=True)
                power = decode(next(p for p in POINTS if p.kind == "battery"),
                               good.registers, None, frame=True)
            self.monitors[bank.id].observe(
                value, read_at=good.read_at if good else None, now=now,
                power_w=power if isinstance(power, float) and reading is not None else None,
                capacity_kwh=bank.usable_capacity_kwh, valid=valid, fallback_active=fallback,
            )

    def bank(self, bank_id: str) -> BankTrust:
        """Live masking also applies between observation ticks and before the first read."""
        monitor = self.monitors.get(bank_id)
        source = self._source(bank_id)
        if monitor is None or source is None or not self._owned():
            return BankTrust()
        view = monitor.view
        native = self.coordinator.native
        good = self._good.get(bank_id)
        if native.reason(source) is not None or (
                good is not None and good.epoch != native.transport(source).connection_epoch):
            return BankTrust(view.value, view.read_at, "untrusted", "read_failed")
        current = native.reading(source, "input_33132")
        if current is not None and current.registers is not None:
            value = decode(next(p for p in POINTS if p.address == 33139),
                           current.registers, None, frame=True)
            if value is None:
                return BankTrust(None, current.read_at, "untrusted", "out_of_range")
        if view.read_at is not None and self.policy is not None:
            age = datetime.now(UTC) - view.read_at
            if age < timedelta(0):
                return BankTrust(view.value, view.read_at, "untrusted", "report_time_future")
            if age > timedelta(minutes=self.policy.soc_max_report_age_minutes):
                return BankTrust(view.value, view.read_at, "untrusted", "stale")
        state = self.coordinator.authority.state
        if (view.trust == "trusted" and state is not None and state.fallback.active
                and "soc_integrity" in state.fallback.causes):
            return BankTrust(view.value, view.read_at, "recovering", "ok")
        return view

    def trusted(self, bank_id: str) -> bool:
        return self.bank(bank_id).trust == "trusted"

    def increases_allowed(self, bank_id: str) -> bool:
        return self.trusted(bank_id)

    @property
    def fallback_requested(self) -> bool:
        return self._owned() and any(m.fallback_requested for m in self.monitors.values())

    @property
    def recovery_ready(self) -> bool:
        return self._owned() and bool(self.monitors) and all(
            self.bank(key).reason == "ok" and (m.recovery_ready or self.trusted(key))
            for key, m in self.monitors.items())

    @property
    def all_trusted(self) -> bool:
        return bool(self.monitors) and all(self.trusted(key) for key in self.monitors)

    @property
    def recovered(self) -> bool:
        state = self.coordinator.authority.state
        return (state is not None and "soc_integrity" not in state.fallback.causes
                and self.all_trusted)

    def snapshot(self) -> list[dict[str, object]]:
        banks: list[dict[str, object]] = []
        if self.topology is None:
            return banks
        for bank in self.topology.banks:
            view = self.bank(bank.id)
            banks.append({"id": bank.id, "soc": {
                "value": view.value, "read_at": view.read_at.isoformat() if view.read_at else None,
                "trust": view.trust, "reason": view.reason,
            } if self._source(bank.id) else None})
        return banks
