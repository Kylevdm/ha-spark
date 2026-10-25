"""Shared entity migration helpers for the add-on's former pushed states."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er


@callback
def async_clear_unregistered_state(hass: HomeAssistant, entity_id: str) -> None:
    """Free a requested id without touching any registered entity's state."""
    if er.async_get(hass).async_get(entity_id) is None:
        hass.states.async_remove(entity_id)
