"""Poll the add-on and enforce a session-local liveness deadline independently.

A lost link changes visibility only. This integration owns no authority, fallback
or hardware write path. Unloading fences off replies from the previous discovery.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import wire
from .channel import Outcome, PollResult, async_poll
from .const import DOMAIN
from .topology import Topology, validate_topology
from .wire import Summary, build_snapshot

if TYPE_CHECKING:
    from .solcast_coordinator import SolcastCoordinator

_LOGGER = logging.getLogger(__name__)


class SparkCoordinator(DataUpdateCoordinator[bool]):
    """Own connection acceptance, fixed outcome records and the deadline timer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any], release_version: str) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="ha-spark",
                         update_interval=timedelta(seconds=30))
        self.entry = entry
        self.solcast: SolcastCoordinator | None = None
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

    async def _async_update_data(self) -> bool:
        if not self._running or self._ended:
            return self.connected
        data = self.entry.data
        api_version = self.api_version
        envelope = None
        topology, major_loads = self._topology_snapshot()
        try:
            snapshot = build_snapshot(site_id=data["site_id"],
                                      release_version=self.release_version,
                                      sent_at=dt_util.utcnow(), api_version=api_version,
                                      solar_forecast=self._solar_forecast(api_version),
                                      topology=topology, major_loads=major_loads)
        except (KeyError, ValueError):
            # Raised text would carry the stored value; HA logs an update's exceptions.
            result = PollResult(Outcome.MALFORMED)
        else:
            session = async_get_clientsession(self.hass)
            url = f"http://{data['host']}:{data['port']}/integration/heartbeat"
            result = await async_poll(session, url=url, token=data["auth_token"], snapshot=snapshot)
            envelope = result.envelope
            if result.outcome is Outcome.VERSION_REFUSED:
                assert envelope is not None and envelope.supported_api_versions is not None
                common = wire.SUPPORTED_API_VERSIONS & envelope.supported_api_versions
                if common:
                    if not self._running or self._ended or self.entry.data is not data:
                        return self.connected
                    api_version = max(common)
                    try:
                        snapshot = build_snapshot(site_id=data["site_id"],
                                                  release_version=self.release_version,
                                                  sent_at=dt_util.utcnow(), api_version=api_version,
                                                  solar_forecast=self._solar_forecast(api_version),
                                                  topology=topology, major_loads=major_loads)
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
            ir.async_delete_issue(self.hass, DOMAIN, "api_version_skew")
            self.connected = True
            self.last_counted = now
            self.summary = summary
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
