"""Poll the add-on and enforce a session-local liveness deadline independently.

Native authority is local only on integration-owned sites; the legacy mirror
never grants local write permission. Ownership changes through the durable
Observe-only handshake, not a snapshot grant. Unloading fences off replies
from the previous discovery. Apply commands use the verified charge boundary;
the production guard shares that boundary; native holds own protection steps.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import wire
from .authority import AuthorityStore
from .authority_control import AuthorityControl
from .authority_store import ExportSupervision
from .channel import Outcome, PollResult, async_fetch_commands, async_poll, async_post_result
from .command_dispatch import CommandConsumer
from .commands import (
    Command,
    CommandResult,
    EndSupervisionPayload,
    FetchRequest,
    ManualPayload,
    ReprobePayload,
    SupervisionPayload,
    TransitionPayload,
)
from .const import DOMAIN
from .native import NativeDevices
from .native_clock import NativeClock
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
from .native_fallback_control import NativeFallback
from .native_holds import NativeHolds, NativeTransitionWriter
from .native_power import polled_power
from .native_soc import NativeSoc
from .native_write import NativeChargeBoundary
from .ownership import OwnershipStore
from .topology import Topology, validate_topology
from .transitions import Preconditions, TransitionRequest
from .wire import Summary, build_snapshot

if TYPE_CHECKING:
    from .guard_loop import SupplyGuardLoop
    from .solcast_coordinator import SolcastCoordinator

_LOGGER = logging.getLogger(__name__)


class SparkCoordinator(DataUpdateCoordinator[bool]):
    """Own connection acceptance, fixed outcome records and the deadline timer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any], release_version: str) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="ha-spark",
                         update_interval=timedelta(seconds=30))
        self.entry = entry
        self.authority = AuthorityStore(hass, entry.entry_id, self.async_update_listeners)
        self.control = AuthorityControl(entry, self.authority)
        self.ownership = OwnershipStore(
            hass, entry.entry_id, self.authority, lambda: entry.options.get("topology"),
        )
        self.control.owner_source = lambda: (
            self.ownership.verified and not self.ownership.reconcile
            and self.ownership.owner == "integration")
        self.native = NativeDevices(
            hass, entry,
            owned=lambda: self.ownership.verified and self.ownership.owner == "integration",
            # The last accepted overlay report survives add-on liveness loss;
            # local protection must keep working without the add-on. Every new
            # accepted report still rechecks this fence, including a missing block.
            overlay=lambda: self.summary.legacy_overlay if self.summary else None,
            changed=self.async_update_listeners,
        )
        self.clock = NativeClock(self)
        self.soc = NativeSoc(self)
        self.control.preconditions = lambda: Preconditions(
            devices_validated=self._devices_validated(),
            evidence_configured=self.holds.evidence.configured,
            limits_confirmed=self._limits_confirmed(),
            fallback_valid=self.fallback.valid(dt_util.utcnow()),
            live_session=(self.connected
                          and self.control.permits_non_protection(dt_util.utcnow())),
            soc_trusted=self.soc.all_trusted, soc_recovered=self.soc.recovered,
        )
        self.solcast: SolcastCoordinator | None = None
        self.guard: SupplyGuardLoop | None = None
        self.writes = NativeChargeBoundary(self)
        self.holds = NativeHolds(self)
        self.fallback = NativeFallback(self)
        self.native.held_remapped = self.holds.remapped
        self.control.writer = NativeTransitionWriter(self)
        self.control.manual_resumed = self.writes.async_reverify_windows
        self.control.write_barrier = self.writes.async_barrier
        self.release_version = release_version
        self.api_version = max(wire.SUPPORTED_API_VERSIONS)
        self.addon_api_version: int | None = None
        self.connected = False
        self.last_counted: datetime | None = None
        # The latest counted summary; entities show it only while connected.
        self.summary: Summary | None = None
        self.last_outcome: Outcome | None = None
        self.last_outcome_at: datetime | None = None
        self._running = True
        self._ended = False
        self._cancel_deadline: Callable[[], None] | None = None
        # Receiver incarnation is distinct from the add-on's per-boot bearer session.
        # A fresh incarnation makes the add-on drop commands from before HA restarted.
        self.integration_session_id = str(uuid4())
        self.command_consumer: CommandConsumer | None = None
        self._command_fetching = False
        self._command_queued = False

    @callback
    def async_commands_pending(self) -> None:
        """A data-free nudge only; never accepts a command from a service caller."""
        if (not self._running or self._ended or not self.connected or self.api_version < 4
                or not self.control.owned()):
            return
        if self._command_fetching:
            self._command_queued = True
            return
        self._command_fetching = True
        self.entry.async_create_background_task(
            self.hass, self._async_commands(), "ha-spark command fetch",
        )

    async def _async_dispatch_command(self, command: Command) -> CommandResult:
        base = CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome="refused", reason="not_available")
        if command.kind == "apply":
            return await self.writes.async_apply(command)
        if not self.control.owned():
            return base.model_copy(update={"reason": "not_owner"})
        if command.kind == "sync_clock":
            result = await self.clock.async_command(command.epoch)
            return result.model_copy(update={"session_id": command.session_id,
                                             "sequence": command.sequence})
        if command.kind == "reprobe":
            # #574's admin seam: identity and caching reads only, no register writes.
            reprobe = ReprobePayload.model_validate(command.payload)
            if not await self.async_reprobe(reprobe.inverter_id):
                return base.model_copy(update={"reason": "native_device_missing"})
            return base.model_copy(update={"outcome": "applied", "reason": None})
        if command.kind == "supervise_export":
            return await self._async_supervise_export(command)
        if command.kind == "end_supervision":
            return await self._async_end_supervision(command)
        if command.kind not in ("transition", "manual"):
            return base
        payload = (TransitionPayload.model_validate(command.payload)
                   if command.kind == "transition"
                   else ManualPayload.model_validate(command.payload))
        if payload.expected_epoch != command.epoch:
            # The locked controller checks only expected_epoch; the two must agree.
            return base.model_copy(update={"reason": "stale_authority"})
        request = TransitionRequest.model_validate(payload.model_dump())
        zone = self.control.household_zone()
        if zone is None and request.target == "manual" and request.duration == "until_tomorrow":
            return base.model_copy(update={"reason": "invalid_household_zone"})
        from .owner_services import _manual_time

        decision = await self.control.async_request(
            request, now=dt_util.utcnow(), zone=zone or ZoneInfo("UTC"),
            at=_manual_time(self.entry),
        )
        if decision.refusal is not None:
            return CommandResult.model_validate({**base.model_dump(), "reason": decision.refusal,
                                                  "reasons": list(decision.reasons)})
        state = decision.state
        if state is None:
            return base.model_copy(update={"reason": "authority_unknown"})
        return base.model_copy(update={"outcome": "applied", "reason": None, "epoch": state.epoch})

    async def _async_admin_user(self, payload: Any) -> str | None:
        """CLI is the local owner; an ingress id must resolve to a non-system admin."""
        if payload.requester_kind == "cli":
            return "cli"
        if payload.user_id is None:
            return None
        user = await self.hass.auth.async_get_user(payload.user_id)
        if user is None or not user.is_admin or user.system_generated:
            return None
        return user.id

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
        if not await self.authority.async_save(state.model_copy(update={
            "export_supervisions": kept + tuple(rows), "last_changed_at": now,
        })):
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
            if state is None or not state.export_supervisions:
                return
            kept = tuple(row for row in state.export_supervisions if row.ends_at > now)
            if len(kept) == len(state.export_supervisions):
                return
            await self.authority.async_save(state.model_copy(update={
                "export_supervisions": kept, "last_changed_at": now,
            }))

    async def async_retry_export_records(self, now: datetime) -> None:
        """Retry every non-live, non-cleared record while committed Optimise."""
        async with self.control.lock:
            state = self.authority.state
            if (state is None or state.committed_mode != "optimise"
                    or not self.control.owned() or state.manual_session is not None
                    or state.fallback.active
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
                await self.writes.async_clear_export(inverter.id, epoch=state.epoch)

    async def _async_commands(self) -> None:
        """At most one HTTP fetch plus one queued fetch, including forged nudges."""
        try:
            while True:
                self._command_queued = False
                data = self.entry.data
                if (not self._running or self._ended or not self.connected
                        or not self.control.owned()):
                    return
                session_id = data["session_id"]
                consumer = self.command_consumer
                if consumer is None or consumer.session_id != session_id:
                    consumer = CommandConsumer(
                        session_id, lambda: (self.authority.state.epoch
                                            if self.authority.state is not None else None),
                        self._async_dispatch_command,
                    )
                    self.command_consumer = consumer
                url = f"http://{data['host']}:{data['port']}/integration/commands"
                session = async_get_clientsession(self.hass)
                reply = await async_fetch_commands(
                    session, url=url + "/fetch", token=data["auth_token"],
                    request=FetchRequest(session_id=session_id,
                                         integration_session_id=self.integration_session_id,
                                         last_result_sequence=consumer.last_sequence),
                )
                if not self._running or self._ended or self.entry.data is not data:
                    return
                if reply is not None:
                    for command in reply.commands:
                        if (not self._running or self._ended or self.entry.data is not data
                                or not self.control.owned()):
                            return
                        if reply.session_id != session_id:
                            result = CommandResult(session_id=command.session_id,
                                                   sequence=command.sequence, outcome="refused",
                                                   reason="stale_session")
                        else:
                            result = await consumer.async_consume(command)
                        if not self._running or self._ended or self.entry.data is not data:
                            return
                        await async_post_result(session, url=url + "/result",
                                                token=data["auth_token"], result=result)
                if not self._command_queued:
                    return
        finally:
            self._command_fetching = False

    def _devices_validated(self) -> bool:
        """Every inverter of the current topology, matched against current probe evidence."""
        topology = validate_topology(self.entry.options.get("topology"))
        return isinstance(topology, Topology) and bool(topology.inverters) and all(
            i.transport == "native" and self.native.reason(i) is None
            and polled_power(self.native, i, dt_util.utcnow()) is not None
            for i in topology.inverters
        )

    def _limits_confirmed(self) -> bool:
        topology = validate_topology(self.entry.options.get("topology"))
        return isinstance(topology, Topology) and all(i.confirmed for i in topology.inverters)

    async def async_reprobe(self, inverter_id: str) -> bool:
        """Native admin command seam; authentication belongs to the caller boundary."""
        found = await self.native.async_reprobe(inverter_id)
        # A Re-probe that changes the identity invalidates that inverter's bound
        # until a new supervise_export (Q22a): its supervision rows are dropped.
        async with self.control.lock:
            state = self.authority.state
            if state is not None:
                probe = self.native.probes.get(inverter_id)
                identity = tuple(probe.identity) if probe is not None else None
                kept = tuple(row for row in state.export_supervisions
                             if row.inverter_id != inverter_id or row.identity == identity)
                if len(kept) != len(state.export_supervisions):
                    await self.authority.async_save(state.model_copy(update={
                        "export_supervisions": kept, "last_changed_at": dt_util.utcnow()}))
        return found

    @callback
    def _clear_deadline(self) -> None:
        if self._cancel_deadline is not None:
            self._cancel_deadline()
            self._cancel_deadline = None

    @callback
    def _expire(self, _now: datetime) -> None:
        self._cancel_deadline = None
        self.connected = False
        self.fallback.liveness_lost()
        self.data = False
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Stop applying results before cancelling timers and scheduled polls."""
        self._running = False
        self.connected = False
        self._clear_deadline()
        await self.native.async_close()
        await super().async_shutdown()

    @callback
    def async_stop(self) -> None:
        """Fence off replies the moment unloading starts, before platforms go."""
        self._running = False
        self.native.polling = False

    def _solar_forecast(self, api_version: int) -> dict[str, object] | None:
        """The snapshot's forecast field; a failure to build it never blocks the heartbeat."""
        if api_version < 2 or self.solcast is None:
            return None
        try:
            return self.solcast.snapshot_forecast(dt_util.utcnow())
        except Exception:  # noqa: BLE001 - its text may never reach HA's update logging
            # Still configured: the add-on stays native and plans with zero solar.
            return {"status": "stale", "fetched_at": None, "calls_used": 0,
                    "calls_limit": self.solcast.calls_limit, "slots": []}

    def _topology_snapshot(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        """Read stored configuration and source timestamps; never echo invalid state."""
        options = self.entry.options
        if "topology_revision" not in options:
            return {"configured": False}, []
        raw = options.get("topology")
        block = {"configured": True, "revision": options["topology_revision"], "topology": raw}
        topology = validate_topology(raw)
        readings: list[dict[str, object]] = []
        if isinstance(topology, Topology):
            for load in topology.major_loads:
                state = self.hass.states.get(load.power_entity) if load.power_entity else None
                draw = None
                reported = None
                if state is not None:
                    reported = state.last_reported.astimezone(UTC).isoformat()
                    try:
                        value = float(state.state)
                        unit = state.attributes.get("unit_of_measurement")
                        if isfinite(value) and value >= 0 and unit in ("W", "kW"):
                            draw = value / 1000 if unit == "W" else value
                    except (ValueError, TypeError, OverflowError):
                        pass
                readings.append({"id": load.id, "draw_kw": draw, "reported_at": reported})
        return block, readings

    def _guard_snapshot(self) -> dict[str, object] | None:
        """The v3 guard block; a failure to build it never blocks the heartbeat.

        The revision is the topology revision the guard was started with: options
        edits reload the entry, so the two can never disagree. Ids are the
        topology's connection ids, or the add-on drops the whole block.
        """
        guard = self.guard
        if guard is None or not guard.started or not guard.states:
            return None
        try:
            revision = self.entry.options.get("topology_revision")
            if type(revision) is not int or revision < 0:
                return None
            allowances = guard.allowance_kw
            connections: list[dict[str, object]] = []
            for connection in guard.topology.connections:
                allowance = allowances.get(connection.id)
                if allowance is not None and not (isfinite(allowance) and 0 <= allowance <= 60):
                    # Never invented or clamped: an out-of-contract value sends no
                    # block, which the add-on reads as "no guard information".
                    return None
                connections.append({"id": connection.id, "allowance_kw": allowance})
            return {
                "limiting": guard.limiting,
                "connections": connections,
                "restores_left": min(guard.restores_left.values()),
                "revision": revision,
            }
        except Exception:  # noqa: BLE001 - its text may never reach HA's update logging
            return None

    def _ownership_snapshot(self, snapshot: dict[str, object]) -> None:
        """Project local validation without granting hardware permission."""
        if snapshot["api_version"] != 4:
            return
        owner = self.ownership.owner if self.ownership.verified else "pending_integration"
        snapshot["authority_owner"] = owner
        guard_block = snapshot.get("supply_guard")
        if self.guard is not None and isinstance(guard_block, dict):
            connections = guard_block.get("connections")
            if isinstance(connections, list):
                for connection in connections:
                    if isinstance(connection, dict) and isinstance(connection.get("id"), str):
                        connection["writer"] = self.guard.owns_connection(connection["id"])
        topology = validate_topology(self.entry.options.get("topology"))
        if isinstance(topology, Topology):
            devices: list[dict[str, object]] = []
            for inverter in topology.inverters:
                reason = (self.native.reason(inverter) if inverter.transport == "native"
                          else "unvalidated_identity")
                device: dict[str, object] = {
                    "id": inverter.id, "transport": inverter.transport,
                    "validation": "read_only" if reason else "validated", "capabilities": [],
                }
                if reason:
                    device["reason"] = reason
                devices.append(device)
            snapshot["devices"] = devices
        if owner != "integration":
            return
        state = self.authority.state
        if state is None:
            # Omit the required authority block: the add-on drops this snapshot
            # whole. Never invent an identity, epoch or mode for damaged storage.
            snapshot.pop("authority_owner", None)
            return
        transition = state.transition
        snapshot["authority"] = {
            "mode": state.committed_mode if state else "observe",
            "epoch": state.epoch if state else 1,
            "store_identity": str(state.store_id) if state else self.entry.entry_id,
            "transition": ({"target": transition.target, "steps": list(transition.steps),
                            "failing_actions": list(transition.failing_actions)}
                           if transition else None),
            "manual_session": (state.manual_session.model_dump(mode="json")
                               if state and state.manual_session else None),
            "fallback": {"active": state.fallback.active if state else False,
                         "causes": list(state.fallback.causes) if state else []},
            "store_state": ("ok" if state else "save_failed"
                            if self.authority.status == "save_failed" else "unknown"),
        }
        snapshot["holds"] = [{"cause": h.cause, "device_id": h.device.power_switch,
                              "confirmed": h.confirmed,
                              "trusted_end": h.trusted_end.isoformat() if h.trusted_end else None}
                             for h in state.holds]
        evidence = self.holds.evidence.options()
        if evidence is not None and evidence.configured:
            snapshot["evidence"] = evidence.snapshot()
        snapshot["banks"] = self.soc.snapshot()
        if isinstance(topology, Topology):
            try:
                snapshot["export"] = self._export_snapshot(state, topology)
            except Exception:  # noqa: BLE001 - a bad export view never blocks the heartbeat
                snapshot.pop("export", None)

    def _export_snapshot(self, state: Any, topology: Topology) -> list[dict[str, object]]:
        from .topology import effective_limits

        limits = effective_limits(topology)
        facts = self._export_facts(topology)
        banks = {bank.id: bank for bank in topology.banks}
        years = state.export_supervisions
        entries: list[dict[str, object]] = []
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
                {"id": str(row.id),
                 "kind": ("clock_hold" if row.kind == "retryable_clock_hold" else row.kind),
                 "at": row.at.isoformat(),
                 "reason": row.reason}
                for row in state.export_transitions if row.inverter_id == inverter.id
            ]
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
        return entries

    async def _async_update_data(self) -> bool:
        if not self._running or self._ended:
            return self.connected
        if not self.ownership.loaded:
            await self.authority.async_load()
            await self.ownership.async_load()
        if not self.ownership.verified:
            # An unverified pending record must not even request a handover.
            self.connected = False
            self.last_outcome = Outcome.MALFORMED
            self.last_outcome_at = dt_util.utcnow()
            return False
        self.soc.observe(dt_util.utcnow())
        data = self.entry.data
        api_version = self.api_version
        envelope = None
        topology, major_loads = self._topology_snapshot()
        try:
            snapshot = build_snapshot(site_id=data["site_id"],
                                      release_version=self.release_version,
                                      sent_at=dt_util.utcnow(), api_version=api_version,
                                      solar_forecast=self._solar_forecast(api_version),
                                      topology=topology, major_loads=major_loads,
                                      supply_guard=self._guard_snapshot())
        except (KeyError, ValueError):
            # Raised text would carry the stored value; HA logs an update's exceptions.
            result = PollResult(Outcome.MALFORMED)
        else:
            session = async_get_clientsession(self.hass)
            url = f"http://{data['host']}:{data['port']}/integration/heartbeat"
            self._ownership_snapshot(snapshot)
            result = await async_poll(session, url=url, token=data["auth_token"], snapshot=snapshot)
            envelope = result.envelope
            if result.outcome is Outcome.VERSION_REFUSED:
                assert envelope is not None and envelope.supported_api_versions is not None
                common = wire.SUPPORTED_API_VERSIONS & envelope.supported_api_versions
                if common:
                    if not self._running or self._ended or self.entry.data is not data:
                        return self.connected
                    api_version = max(common)
                    if api_version < 4:
                        await self.ownership.async_abort("ownership_peer_version")
                    try:
                        snapshot = build_snapshot(site_id=data["site_id"],
                                                  release_version=self.release_version,
                                                  sent_at=dt_util.utcnow(), api_version=api_version,
                                                  solar_forecast=self._solar_forecast(api_version),
                                                  topology=topology, major_loads=major_loads,
                                                  supply_guard=self._guard_snapshot())
                    except (KeyError, ValueError):
                        result = PollResult(Outcome.MALFORMED)
                    else:
                        result = await async_poll(
                            session, url=url, token=data["auth_token"], snapshot=snapshot,
                        )
                    envelope = result.envelope or envelope
                if result.outcome is Outcome.VERSION_REFUSED:
                    result = PollResult(Outcome.VERSION_SKEW, envelope=envelope)
        # A new discovery replaces entry.data at once and reloads later: a reply to the old
        # connection data must not count against the new session.
        if not self._running or self._ended or self.entry.data is not data:
            return self.connected
        self.api_version = api_version
        if envelope is not None:
            self.addon_api_version = envelope.api_version
        now = dt_util.utcnow()
        outcome = result.outcome
        summary = result.summary
        if summary is not None:
            if summary.session_id != data["session_id"]:
                outcome = Outcome.SESSION_MISMATCH
            elif summary.loop_tick is None:
                outcome = Outcome.NULL_TICK
            elif summary.loop_tick < now - timedelta(seconds=120):
                outcome = Outcome.STALE_TICK
            elif summary.loop_tick > now + timedelta(seconds=5):
                outcome = Outcome.FUTURE_TICK
        self.last_outcome = outcome
        self.last_outcome_at = now
        self.fallback.observe_outcome(outcome, now)
        if outcome is Outcome.COUNTED:
            if self.control.owned() and summary is not None:
                await self.control.async_new_session(summary.session_id, now=now)
            ir.async_delete_issue(self.hass, DOMAIN, "api_version_skew")
            self.connected = True
            self.last_counted = now
            self.summary = summary
            if summary is not None:
                await self.ownership.async_advance(summary)
            self.native.recheck()
            self.async_commands_pending()
            self._clear_deadline()
            self._cancel_deadline = async_call_later(self.hass, 120, self._expire)
            if (self.solcast is not None and summary is not None
                    and summary.charge_window_start is not None):
                # Even a hung quota store must not delay heartbeat completion.
                self.entry.async_create_background_task(
                    self.hass,
                    self.solcast.async_learn_window_start(summary.charge_window_start),
                    "Solcast charge window",
                )
        elif outcome in {
            Outcome.UNAUTHORIZED, Outcome.SESSION_MISMATCH,
            Outcome.VERSION_SKEW, Outcome.INCOMPATIBLE,
        }:
            self._ended = True
            self.connected = False
            self._clear_deadline()
            if outcome in {Outcome.VERSION_SKEW, Outcome.INCOMPATIBLE}:
                assert envelope is not None
                ir.async_create_issue(
                    self.hass, DOMAIN, "api_version_skew", is_fixable=False,
                    severity=ir.IssueSeverity.ERROR, translation_key="api_version_skew",
                    translation_placeholders={
                        "addon_release": envelope.release_version,
                        "addon_api_version": str(envelope.api_version),
                        "integration_api_version": str(self.api_version),
                        "supported": ", ".join(map(str, sorted(wire.SUPPORTED_API_VERSIONS))),
                        "addon_supported": (
                            ", ".join(map(str, sorted(envelope.supported_api_versions)))
                            if envelope.supported_api_versions is not None else "unknown"
                        ),
                    },
                )
        return self.connected
