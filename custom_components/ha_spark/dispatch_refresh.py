"""Request Octopus dispatch updates without blocking or changing protection evidence."""
import asyncio
import logging
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.core import callback

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator

_LOGGER = logging.getLogger(__name__)
_DOMAIN = "octopus_energy"
_SERVICE = "refresh_intelligent_dispatches"


class DispatchRefresh:
    def __init__(self, coordinator: "SparkCoordinator") -> None:
        self._coordinator = coordinator
        self._last_call: float | None = None
        self._ev_values: dict[str, str] = {}
        self._ev_changed = False
        self._task: asyncio.Task[None] | None = None

    @callback
    def async_tick(self, now: datetime) -> None:
        c = self._coordinator
        if not c.ownership.verified or c.ownership.owner != "integration":
            return
        if not c.hass.services.has_service(_DOMAIN, _SERVICE):
            return
        evidence = c.holds.evidence
        options = evidence.options()
        if options is None or options.live_dispatch is None:
            return
        entity_id = options.live_dispatch.entity_id
        dispatch = c.hass.states.get(entity_id) if entity_id else None
        # A quiet dispatch sensor is why we refresh; only EV evidence needs to be fresh here.
        if (dispatch is None or dispatch.state.strip().lower() in ("unknown", "unavailable")
                or dispatch.attributes.get("manually_refresh_dispatches") is not True):
            return

        freshness = evidence.freshness(now)

        def value(key: str, source_id: str | None) -> str | None:
            reading = freshness.get(key)
            state = c.hass.states.get(source_id) if source_id else None
            return (state.state.strip().lower() if state is not None and reading is not None
                    and reading.validity == "fresh" else None)

        status = value("ev_status", options.ev_status.entity_id if options.ev_status else None)
        plug = value("ev_plug", options.ev_plug.entity_id if options.ev_plug else None)
        # Keep a change observed during the cooldown, but loss of freshness
        # alone is not a value change.
        for key, current in (("ev_status", status), ("ev_plug", plug)):
            if current is not None:
                if self._ev_values.get(key, current) != current:
                    self._ev_changed = True
                self._ev_values[key] = current
        if (status not in ("charging", "boosting", "delivering")
                and plug not in ("ev connected", "charging", "waiting for ev")):
            return
        if self._task is not None and not self._task.done():
            return
        interval = 60 if self._ev_changed else 300
        if self._last_call is not None and monotonic() - self._last_call < interval:
            return
        self._task = c.entry.async_create_background_task(
            c.hass, self._async_refresh(dispatch.entity_id), "Octopus dispatch refresh",
            eager_start=False,
        )

    async def _async_refresh(self, entity_id: str) -> None:
        c = self._coordinator
        if not c.ownership.verified or c.ownership.owner != "integration":
            return
        self._last_call = monotonic()
        self._ev_changed = False
        try:
            async with asyncio.timeout(20):
                await c.hass.services.async_call(
                    _DOMAIN, _SERVICE, {"entity_id": entity_id}, blocking=True,
                )
        except TimeoutError:
            _LOGGER.debug("Octopus dispatch refresh timed out")
        except Exception:
            _LOGGER.debug("Octopus dispatch refresh failed")
