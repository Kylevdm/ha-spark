"""Poll the add-on and enforce a session-local liveness deadline independently.

Native authority is local only on integration-owned sites; the legacy mirror
never grants local write permission. Ownership changes through the durable
Observe-only handshake, not a snapshot grant. Unloading fences off replies
from the previous discovery. No native hardware writer is attached here.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any
from uuid import uuid4
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
from .channel import Outcome, PollResult, async_fetch_commands, async_poll, async_post_result
from .command_dispatch import CommandConsumer
from .commands import (
    Command,
    CommandResult,
    FetchRequest,
    ManualPayload,
    ReprobePayload,
    TransitionPayload,
)
from .const import DOMAIN
from .native import NativeDevices
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
        self.native = NativeDevices(
            hass, entry,
            owned=lambda: self.ownership.verified and self.ownership.owner == "integration",
            overlay=lambda: (self.summary.legacy_overlay
                             if self.connected and self.summary else None),
            changed=self.async_update_listeners,
        )
        self.control.preconditions = lambda: Preconditions(
            devices_validated=self._devices_validated(),
        )
        self.solcast: SolcastCoordinator | None = None
        self.guard: SupplyGuardLoop | None = None
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
        if not self.control.owned():
            return base.model_copy(update={"reason": "not_owner"})
        if command.kind == "reprobe":
            # #574's admin seam: identity and caching reads only, no register writes.
            reprobe = ReprobePayload.model_validate(command.payload)
            if not await self.async_reprobe(reprobe.inverter_id):
                return base.model_copy(update={"reason": "native_device_missing"})
            return base.model_copy(update={"outcome": "applied", "reason": None})
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
            for i in topology.inverters
        )

    async def async_reprobe(self, inverter_id: str) -> bool:
        """Native admin command seam; authentication belongs to the caller boundary."""
        return await self.native.async_reprobe(inverter_id)

    @callback
    def _clear_deadline(self) -> None:
        if self._cancel_deadline is not None:
            self._cancel_deadline()
            self._cancel_deadline = None

    @callback
    def _expire(self, _now: datetime) -> None:
        self._cancel_deadline = None
        self.connected = False
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
        snapshot["holds"] = []

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
