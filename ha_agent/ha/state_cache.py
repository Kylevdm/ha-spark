"""In-memory snapshot of Home Assistant entity states.

Seeded from the REST API and kept fresh by the WebSocket event stream. This is
the "sensor-aware" substrate the agent reads before acting.
"""

from __future__ import annotations

from ha_agent.ha.models import EntityState, StateChangedEvent
from ha_agent.ha.rest import HomeAssistantRest
from ha_agent.logging import get_logger

log = get_logger(__name__)


class StateCache:
    """A live, dict-backed cache of entity states."""

    def __init__(self) -> None:
        self._states: dict[str, EntityState] = {}

    async def seed(self, rest: HomeAssistantRest) -> None:
        """Populate the cache from a full REST state dump."""
        states = await rest.get_states()
        self._states = {s.entity_id: s for s in states}
        log.info("State cache seeded with %d entities", len(self._states))

    async def on_state_changed(self, event: StateChangedEvent) -> None:
        """WebSocket listener: apply a ``state_changed`` event to the cache."""
        if event.new_state is None:
            # Entity removed.
            self._states.pop(event.entity_id, None)
        else:
            self._states[event.entity_id] = event.new_state

    def get(self, entity_id: str) -> EntityState | None:
        """Return the cached state for an entity, or ``None``."""
        return self._states.get(entity_id)

    def all(self) -> list[EntityState]:
        """Return all cached states, sorted by entity id."""
        return [self._states[k] for k in sorted(self._states)]

    def by_domain(self, domain: str) -> list[EntityState]:
        """Return all cached states within a domain."""
        return [s for s in self.all() if s.domain == domain]

    def __len__(self) -> int:
        return len(self._states)
