"""Event-scoped native export supervision and snapshot projection."""
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from homeassistant.util import dt as dt_util

from . import native_export_log
from .authority_store import ExportSupervision, GatewayClaim
from .commands import (
    Command,
    CommandResult,
    EndSupervisionPayload,
    SupervisionPayload,
)
from .native_export import (
    DeviceExportFact,
    bound_refusal,
    claim_from_raw,
    event_from_raw,
    latest_latch,
    latest_record,
    ratings_refusal,
    record_is_live,
    site_export_limit_w,
    snapshot_event,
)
from .topology import Topology, validate_topology

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .authority import AuthorityStore
    from .authority_control import AuthorityControl
    from .guard_loop import SupplyGuardLoop
    from .native import NativeDevices
    from .native_write import NativeChargeBoundary


class ExportSupervisor:
    """Durable export supervision under the coordinator's control lock."""

    if TYPE_CHECKING:
        entry: ConfigEntry[Any]
        hass: HomeAssistant
        authority: AuthorityStore
        control: AuthorityControl
        native: NativeDevices
        guard: SupplyGuardLoop | None
        writes: NativeChargeBoundary
        emitted_transitions: frozenset[str]

        async def _async_admin_user(self, payload: Any) -> str | None:
            ...

    def _export_facts(self, topology: Topology) -> dict[str, DeviceExportFact]:
        raw_claims = self.entry.options.get("gateway_claims")
        claims = raw_claims if isinstance(raw_claims, list) else []
        facts: dict[str, DeviceExportFact] = {}
        for inverter in topology.inverters:
            if inverter.transport != "native":
                facts[inverter.id] = DeviceExportFact(False)
                continue
            claim = next((claim_from_raw(raw) for raw in claims
                          if isinstance(raw, dict)
                          and raw.get("host") == inverter.gateway_host
                          and raw.get("port") == inverter.gateway_port
                          and raw.get("unit_id") == inverter.gateway_unit_id), None)
            probe = self.native.probes.get(inverter.id)
            identity = tuple(probe.identity) if probe is not None else None
            if identity is not None and len(identity) != 3:
                identity = None
            facts[inverter.id] = DeviceExportFact(
                validated=self.native.reason(inverter) is None,
                identity=identity, claim=claim,
            )
        return facts

    def _topology_and_limits(self) -> tuple[Topology | None, Any]:
        from .topology import effective_limits

        topology = validate_topology(self.entry.options.get("topology"))
        if not isinstance(topology, Topology):
            return None, None
        return topology, effective_limits(topology)

    async def _async_supervise_export(self, command: Command) -> CommandResult:
        async with self.control.lock:
            return await self._async_supervise_export_locked(command)

    async def _async_supervise_export_locked(self, command: Command) -> CommandResult:
        base = CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome="refused")
        payload = SupervisionPayload.model_validate(command.payload)
        confirming_user = await self._async_admin_user(payload)
        if confirming_user is None:
            return base.model_copy(update={"reason": "not_admin"})
        if not self.control.owned():
            return base.model_copy(update={"reason": "not_owner"})
        state = self.authority.state
        if state is None:
            return base.model_copy(update={"reason": "authority_unknown"})
        if command.epoch != state.epoch:
            return base.model_copy(update={"reason": "stale_authority"})
        event = event_from_raw(payload.event)
        if event is None:
            return base.model_copy(update={"reason": "invalid_intent"})
        now = dt_util.utcnow()
        if (state.committed_mode != "optimise" or state.manual_session is not None
                or state.fallback.active
                or state.transition is not None and state.transition.target != "optimise"):
            return base.model_copy(update={"reason": "mode"})
        topology, limits = self._topology_and_limits()
        guard = self.guard
        if topology is None or limits is None:
            return base.model_copy(update={"reason": "limits"})
        if guard is not None:
            if not guard.boundary_open:
                return base.model_copy(update={"reason": "limits"})
            topology, limits = guard.topology, guard.limits
        facts = self._export_facts(topology)
        reason = ratings_refusal(event, payload.ratings_w, topology, limits, facts, now=now)
        if reason is not None:
            return base.model_copy(update={"reason": reason})
        supervision_id = uuid4()
        rows: list[ExportSupervision] = []
        for inverter_id in payload.ratings_w:
            fact = facts[inverter_id]
            identity = fact.identity
            claim = fact.claim
            if identity is None or len(identity) != 3 or claim is None:
                return base.model_copy(update={"reason": "export_bound_unverified"})
            rows.append(ExportSupervision(
                inverter_id=inverter_id, id=supervision_id, event=event,
                confirming_user_id=confirming_user, epoch=state.epoch,
                ends_at=event.end, rating_w=payload.ratings_w[inverter_id],
                identity=(identity[0], identity[1], identity[2]), claim=claim,
            ))
        # Re-supervising the same event replaces its rows; other events stay.
        kept = tuple(row for row in state.export_supervisions if row.event != event)
        candidate = state.model_copy(update={
            "export_supervisions": kept + tuple(rows), "last_changed_at": now,
        })
        for row in rows:
            candidate = native_export_log.with_append(
                candidate, inverter_id=row.inverter_id, event=event,
                kind="accepted", at=now,
            )
        if not await self.authority.async_save(candidate):
            return base.model_copy(update={"reason": "authority_unknown"})
        return base.model_copy(update={"outcome": "applied", "reason": None,
                                       "epoch": state.epoch})

    async def _async_end_supervision(self, command: Command) -> CommandResult:
        async with self.control.lock:
            return await self._async_end_supervision_locked(command)

    async def _async_end_supervision_locked(self, command: Command) -> CommandResult:
        base = CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome="refused")
        payload = EndSupervisionPayload.model_validate(command.payload)
        if await self._async_admin_user(payload) is None:
            return base.model_copy(update={"reason": "not_admin"})
        if not self.control.owned():
            return base.model_copy(update={"reason": "not_owner"})
        state = self.authority.state
        if state is None:
            return base.model_copy(update={"reason": "authority_unknown"})
        if command.epoch != state.epoch:
            return base.model_copy(update={"reason": "stale_authority"})
        try:
            supervision_id = UUID(payload.supervision_id)
        except ValueError:
            return base.model_copy(update={"reason": "invalid_intent"})
        dropped = tuple(row for row in state.export_supervisions
                        if row.id == supervision_id)
        kept = tuple(row for row in state.export_supervisions
                     if row.id != supervision_id)
        if not dropped:
            return base.model_copy(update={"reason": "invalid_intent"})
        now = dt_util.utcnow()
        if not await self.authority.async_save(state.model_copy(update={
            "export_supervisions": kept, "last_changed_at": now,
        })):
            return base.model_copy(update={"reason": "authority_unknown"})
        # Authority to export ended with that save; the clear is a separate
        # cleanup obligation whose failure keeps its records for the tick.
        cleared = True
        for inverter_id in dict.fromkeys(row.inverter_id for row in dropped):
            result = await self.writes.async_clear_export(
                inverter_id, epoch=state.epoch)
            cleared = cleared and result.outcome in ("applied", "unchanged")
        return base.model_copy(update={
            "outcome": "applied" if cleared else "unverified",
            "reason": None if cleared else None, "epoch": state.epoch,
        })

    async def async_expire_supervisions(self, now: datetime) -> None:
        """End event-scoped supervisions; the cleanup obligation is separate."""
        async with self.control.lock:
            state = self.authority.state
            if state is None:
                return
            pruned = native_export_log.with_pruned(state, now)
            if pruned is not state:
                if not await self.authority.async_save(pruned):
                    return
                state = self.authority.state
                if state is None:
                    return
            kept = tuple(row for row in state.export_supervisions if row.ends_at > now)
            # A stop latch lasts to its window end and no longer.
            latches = tuple(row for row in state.stop_latches if row.window_end > now)
            if (len(kept) == len(state.export_supervisions)
                    and len(latches) == len(state.stop_latches)):
                return
            await self.authority.async_save(state.model_copy(update={
                "export_supervisions": kept, "stop_latches": latches,
                "last_changed_at": now,
            }))

    async def _async_ack_export_transitions(
        self, acks: tuple[str, ...] | None, now: datetime,
    ) -> None:
        """Acknowledged ids hide/prune rows; they never change control state."""
        if not acks:
            return
        async with self.control.lock:
            state = self.authority.state
            if state is None:
                return
            emitted = [ack for ack in acks if ack in self.emitted_transitions]
            candidate = native_export_log.with_acks(state, emitted, now)
            if candidate is not state:
                await self.authority.async_save(candidate)

    async def async_retry_export_records(self, now: datetime) -> None:
        """Retry every non-live, non-cleared record in any owned non-Observe mode.

        The restart re-verification also runs here first, so a record loaded while
        the device was still unvalidated is never kept from the record alone. A
        fallback entry's own cancel is retried with the transition policy; every
        other cleanup only ever writes a discharge-half zero.
        """
        async with self.control.lock:
            state = self.authority.state
            if (state is None or state.committed_mode == "observe"
                    or not self.control.owned()
                    or state.transition is not None
                    and "cancel_export" in state.transition.steps):
                return
            fallback = state.fallback.active
        await self.writes.async_reconcile_pending(now)
        async with self.control.lock:
            state = self.authority.state
            if (state is None or state.committed_mode == "observe"
                    or not self.control.owned() or state.fallback.active != fallback
                    or state.transition is not None
                    and "cancel_export" in state.transition.steps):
                return
            for inverter in self.native.inverters.values():
                state = self.authority.state
                if state is None:
                    return
                record = latest_record(state.export_records, inverter.id)
                if record is None or record.state == "cleared":
                    continue
                if record_is_live(
                        record, now=now, epoch=state.epoch, store_id=state.store_id,
                        supervisions=state.export_supervisions,
                        stop_latches=state.stop_latches,
                        fallback_active=state.fallback.active):
                    continue
                if state.fallback.active:
                    await self.writes.async_cancel_export(inverter.id, epoch=state.epoch)
                else:
                    await self.writes.async_clear_export(
                        inverter.id, epoch=state.epoch, cleanup=True)

    async def async_drop_stale_bounds(
        self, *, claims: object = None, now: datetime | None = None,
    ) -> bool:
        """Drop supervision rows whose identity or claim can no longer hold (W9-2).

        A re-probe that changed an inverter's identity, or a gateway claim edit,
        ends that inverter's bound. Rows are dropped under the authority lock and
        the in-memory identity latch is cleared only once the save verified.
        """
        changed = set(self.native.identity_changed)
        topology = validate_topology(self.entry.options.get("topology"))
        raw_claims = claims if claims is not None else self.entry.options.get("gateway_claims")
        current: dict[str, GatewayClaim | None] = {}
        if isinstance(topology, Topology):
            for inverter in topology.inverters:
                claim = next((claim_from_raw(raw) for raw in (
                    raw_claims if isinstance(raw_claims, list) else [])
                    if isinstance(raw, dict)
                    and raw.get("host") == inverter.gateway_host
                    and raw.get("port") == inverter.gateway_port
                    and raw.get("unit_id") == inverter.gateway_unit_id), None)
                current[inverter.id] = claim
        async with self.control.lock:
            state = self.authority.state
            if state is None:
                return False
            identities = self.native.identities
            kept = tuple(row for row in state.export_supervisions
                         if row.inverter_id not in changed
                         and identities.get(row.inverter_id, row.identity) == row.identity
                         and (row.inverter_id not in current
                              or current[row.inverter_id] == row.claim))
            if len(kept) == len(state.export_supervisions):
                self.native.identity_changed -= changed
                return True
            saved = await self.authority.async_save(state.model_copy(update={
                "export_supervisions": kept,
                "last_changed_at": now or dt_util.utcnow()}))
            if saved:
                self.native.identity_changed -= changed
            return saved

    def _export_snapshot(self, state: Any, topology: Topology) -> list[dict[str, object]]:
        from .topology import effective_limits

        limits = effective_limits(topology)
        facts = self._export_facts(topology)
        banks = {bank.id: bank for bank in topology.banks}
        years = state.export_supervisions
        entries: list[dict[str, object]] = []
        emitted: set[str] = set()
        for inverter in topology.inverters:
            if inverter.transport != "native":
                continue
            record = latest_record(state.export_records, inverter.id)
            supervision = (next((row for row in reversed(years)
                                 if row.inverter_id == inverter.id
                                 and record is not None and row.event == record.event), None)
                           or next((row for row in reversed(years)
                                    if row.inverter_id == inverter.id), None))
            bound = None
            if supervision is not None:
                fact = facts.get(inverter.id, DeviceExportFact(False))
                connection_id = banks[inverter.bank_id].connection_id
                limit_w = site_export_limit_w(topology, limits, connection_id)
                total_w = sum(row.rating_w for row in years if row.event == supervision.event
                              and banks[next(i.bank_id for i in topology.inverters
                                             if i.id == row.inverter_id)].connection_id
                              == connection_id)
                reason = bound_refusal(supervision, fact, site_limit_w=limit_w,
                                       connection_sum_w=total_w)
                bound = {"watts": supervision.rating_w,
                         "validated": reason is None, "reason": reason}
            latch = latest_latch(state.stop_latches, inverter.id)
            transitions = [
                {"id": str(row.id), "kind": row.kind,
                 "event": snapshot_event(row.event),
                 "at": row.at.isoformat(),
                 "reason": row.reason, "cleared": row.cleared}
                for row in native_export_log.visible(state.export_transitions, inverter.id)
            ]
            emitted.update(str(row["id"]) for row in transitions)
            entries.append({
                "inverter_id": inverter.id,
                "bound": bound,
                "supervision": ({
                    "id": str(supervision.id), "event": snapshot_event(supervision.event),
                    "user_id": supervision.confirming_user_id,
                    "epoch": supervision.epoch,
                    "ends_at": supervision.ends_at.isoformat(),
                } if supervision is not None else None),
                "record": ({
                    "event": snapshot_event(record.event),
                    "window": {"start": record.window.start.isoformat(),
                               "end": record.window.end.isoformat()},
                    "store_identity": str(record.store_id), "epoch": record.epoch,
                    "supervision_id": str(record.supervision_id), "state": record.state,
                    "last_reconcile_at": record.last_reconcile_at.isoformat(),
                    "stop_reason": record.stop_reason,
                    "cleanup_verified": record.cleanup_verified,
                } if record is not None else None),
                "stop_latch": ({
                    "event": snapshot_event(latch.event),
                    "window_end": latch.window_end.isoformat(), "epoch": latch.epoch,
                } if latch is not None else None),
                "transitions": transitions,
            })
        self.emitted_transitions = frozenset(emitted)
        return entries
