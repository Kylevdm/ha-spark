"""Poll the add-on and enforce a session-local liveness deadline independently.

A lost link changes visibility only. This integration owns no authority, fallback
or hardware write path. Unloading fences off replies from the previous discovery.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .channel import Outcome, PollResult, async_poll
from .wire import build_snapshot

_LOGGER = logging.getLogger(__name__)


class SparkCoordinator(DataUpdateCoordinator[bool]):
    """Own connection acceptance, fixed outcome records and the deadline timer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any], release_version: str) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="ha-spark",
                         update_interval=timedelta(seconds=30))
        self.entry = entry
        self.release_version = release_version
        self.connected = False
        self.last_counted: datetime | None = None
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

    async def _async_update_data(self) -> bool:
        if not self._running or self._ended:
            return self.connected
        data = self.entry.data
        try:
            snapshot = build_snapshot(site_id=data["site_id"],
                                      release_version=self.release_version,
                                      sent_at=dt_util.utcnow())
        except (KeyError, ValueError):
            # Raised text would carry the stored value; HA logs an update's exceptions.
            result = PollResult(Outcome.MALFORMED)
        else:
            result = await async_poll(
                async_get_clientsession(self.hass),
                url=f"http://{data['host']}:{data['port']}/integration/heartbeat",
                token=data["auth_token"],
                snapshot=snapshot,
            )
        # A new discovery replaces entry.data at once and reloads later: a reply to the old
        # connection data must not count against the new session.
        if not self._running or self._ended or self.entry.data is not data:
            return self.connected
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
            self.connected = True
            self.last_counted = now
            self._clear_deadline()
            self._cancel_deadline = async_call_later(self.hass, 120, self._expire)
        elif outcome in {Outcome.UNAUTHORIZED, Outcome.SESSION_MISMATCH}:
            self._ended = True
            self.connected = False
            self._clear_deadline()
        # TODO #479: version_refused/incompatible end the session and raise the skew repair.
        return self.connected
