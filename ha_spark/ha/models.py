"""Pydantic models for Home Assistant entities, states, and events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EntityState(BaseModel):
    """A single entity's state as returned by ``/api/states`` or a state event."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    last_changed: datetime | None = None
    last_updated: datetime | None = None

    @property
    def domain(self) -> str:
        """The entity domain (the part before the first dot)."""
        return self.entity_id.split(".", 1)[0]

    @property
    def friendly_name(self) -> str:
        """Human-friendly name, falling back to the entity id."""
        name = self.attributes.get("friendly_name")
        return str(name) if name else self.entity_id


class StateChangedEvent(BaseModel):
    """Payload of a ``state_changed`` event from the WebSocket API."""

    entity_id: str
    old_state: EntityState | None = None
    new_state: EntityState | None = None


class ServiceCall(BaseModel):
    """A request to call a Home Assistant service."""

    domain: str
    service: str
    data: dict[str, Any] = Field(default_factory=dict)
