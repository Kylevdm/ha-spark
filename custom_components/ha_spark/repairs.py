"""Observe-only authority recovery, with no device or add-on calls.

Uses HA's repairs issue_handler.ConfirmRepairFlow confirm-step contract and
helpers.issue_registry issue data to select the affected config entry.
"""

from typing import Any

import probatio
from homeassistant.components.repairs import RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.core import HomeAssistant

from . import SparkConfigEntry


class AuthorityRepairFlow(RepairsFlow):
    """A confirmation, not a mode selector or an escalation path."""

    def __init__(self, entry: SparkConfigEntry | None) -> None:
        super().__init__()
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> RepairsFlowResult:
        entry = self._entry
        if entry is None or not hasattr(entry, "runtime_data"):
            return self.async_abort(reason="entry_unavailable")
        store = entry.runtime_data.authority
        if store.status not in ("unknown", "save_failed"):
            return self.async_abort(reason="authority_known")
        errors: dict[str, str] = {}
        if user_input is not None:
            if await store.async_confirm_observe():
                return self.async_create_entry(data={})
            errors["base"] = "save_failed"
        return self.async_show_form(
            step_id="confirm", data_schema=probatio.Schema({}), errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None,
) -> AuthorityRepairFlow:
    """Resolve the issue's entry without accepting owner-entered paths or modes."""
    entry_id = data.get("entry_id") if data else None
    entry: SparkConfigEntry | None = (
        hass.config_entries.async_get_entry(entry_id) if isinstance(entry_id, str) else None
    )
    return AuthorityRepairFlow(entry)
