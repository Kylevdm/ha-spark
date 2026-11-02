"""Observe-only authority recovery and explicit native inverter clock repair.

Uses HA's repairs issue_handler.ConfirmRepairFlow confirm-step contract and
helpers.issue_registry issue data to select the affected config entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.components.repairs import RepairsFlow
from homeassistant.components.repairs.models import RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from . import SparkConfigEntry


def ownership_switch_refused(hass: HomeAssistant, entry_id: str, reason: str | None) -> None:
    """Expose fixed handover conditions, never topology or peer-entered values."""
    issue_id = f"ownership_switch_refused_{entry_id}"
    if reason is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
    else:
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.ERROR,
            translation_key=reason,
        )


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


class ClockRepairFlow(RepairsFlow):
    """HA's repairs endpoint requires an administrator; confirm one inverter sync."""

    def __init__(self, entry: SparkConfigEntry | None, inverter_id: str | None) -> None:
        super().__init__()
        self._entry = entry
        self._inverter_id = inverter_id

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
        if self._inverter_id not in entry.runtime_data.native.inverters:
            return self.async_abort(reason="native_device_missing")
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._inverter_id is not None
            result = await entry.runtime_data.clock.async_sync(self._inverter_id)
            if result.outcome == "applied":
                return self.async_create_entry(data={})
            errors["base"] = result.reason or result.outcome
        return self.async_show_form(
            step_id="confirm", data_schema=probatio.Schema({}), errors=errors,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None,
) -> AuthorityRepairFlow | ClockRepairFlow:
    """Resolve the issue's entry without accepting owner-entered paths or modes."""
    entry_id = data.get("entry_id") if data else None
    entry: SparkConfigEntry | None = (
        hass.config_entries.async_get_entry(entry_id) if isinstance(entry_id, str) else None
    )
    if issue_id.startswith("clock_drift_"):
        inverter_id = data.get("inverter_id") if data else None
        return ClockRepairFlow(entry, inverter_id if isinstance(inverter_id, str) else None)
    return AuthorityRepairFlow(entry)
