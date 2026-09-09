"""Pydantic models for Home Assistant entities, states, and events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

# Built once: TypeAdapter construction is not free, and this runs per entity read.
_DATETIME_ADAPTER = TypeAdapter(datetime)


class EntityState(BaseModel):
    """A single entity's state as returned by ``/api/states`` or a state event."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    last_changed: datetime | None = None
    last_updated: datetime | None = None
    # When Home Assistant last received a report for this entity, whether or not
    # the value changed. SoC freshness is judged on this and nothing else
    # (`ha_spark/energy/soc_integrity.py`) — an unchanged but actively reported
    # value must stay usable, which `last_changed`/`last_updated` cannot express.
    last_reported: datetime | None = None

    @field_validator("last_reported", mode="before")
    @classmethod
    def _tolerate_bad_report_time(cls, value: Any) -> Any:
        """An unparseable `last_reported` degrades to None rather than failing the read.

        It comes from outside the process and gates actuation, so a malformed
        value must reach the SoC integrity check as "unusable" rather than make
        the whole entity state unreadable for every other consumer.
        `last_changed`/`last_updated` keep their existing strict behaviour.
        """
        if value is None or isinstance(value, datetime):
            return value
        try:
            return _DATETIME_ADAPTER.validate_python(value)
        except ValidationError:
            return None

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
