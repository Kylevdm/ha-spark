"""Config flow for the ha-spark companion integration.

Supervisor discovery links one add-on to a generated site identity. Confirmation
never probes the add-on; subsequent boots refresh the connection silently.
Manual host/port/token waits for phase 10. Solcast credentials are entry-owned;
reconfigure and reauth check them through the same durable per-entry quota gate.
"""

from typing import Any
from uuid import uuid4

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import ADDON_SLUG, DOMAIN
from .solcast import Outcome, async_list_sites
from .solcast_budget import async_get_budget


class HaSparkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ha-spark config flow."""

    VERSION = 1

    _connection: dict[str, Any]

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Accept only our Supervisor add-on and validated connection fields."""
        config = discovery_info.config
        if discovery_info.slug != ADDON_SLUG or not isinstance(config, dict) or not all(
            isinstance(config.get(key), str) and bool(config[key].strip())
            for key in ("host", "auth_token", "session_id")
        ) or type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
            return self.async_abort(reason="invalid_discovery")
        self._connection = {
            key: config[key] for key in ("host", "port", "auth_token", "session_id")
        }
        self._connection["slug"] = ADDON_SLUG
        for flow in self._async_in_progress():
            self.hass.config_entries.flow.async_abort(flow["flow_id"])
        if entries := self._async_current_entries():
            entry = entries[0]
            if entry.data.get("slug") != ADDON_SLUG:
                return self.async_abort(reason="already_configured")
            return self.async_update_reload_and_abort(
                entry, data_updates=self._connection, reason="already_configured"
            )
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the site once, even if confirmations race."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        if user_input is None:
            return self.async_show_form(step_id="confirm")
        site_id = str(uuid4())
        await self.async_set_unique_id(site_id)
        # No await between this final check and creation. Never use discovery's
        # uuid: Supervisor withdrawal removes entries keyed on that uuid.
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        return self.async_create_entry(
            title="ha-spark", data={**self._connection, "site_id": site_id}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "SolcastOptionsFlow":
        """Quota is owner configuration, not a credential."""
        return SolcastOptionsFlow()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Add or replace a key; an empty key requires explicit confirmation."""
        return await self._async_solcast_key(user_input, "reconfigure")

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Ask for a replacement without exposing the rejected key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Replace a rejected Solcast credential."""
        return await self._async_solcast_key(user_input, "reauth_confirm")

    async def _async_solcast_key(
        self, user_input: dict[str, Any] | None, step_id: str,
    ) -> ConfigFlowResult:
        entry = (
            self._get_reauth_entry() if self.source == SOURCE_REAUTH
            else self._get_reconfigure_entry()
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input.get("solcast_api_key", "")
            if not key and self.source != SOURCE_REAUTH:
                return await self.async_step_clear_solcast()
            if not isinstance(key, str) or not key.strip():
                errors["base"] = "auth_failed"
            else:
                gate = async_get_budget(self.hass, entry.entry_id)
                result = await gate.async_request(
                    lambda: async_list_sites(async_get_clientsession(self.hass), key),
                    calls_limit=entry.options.get("calls_limit", 10),
                )
                if result.outcome == Outcome.OK:
                    if await gate.async_set_first_fetch_pending(True):
                        return self.async_update_reload_and_abort(entry, data_updates={
                            "solcast_api_key": key,
                            "solcast_resource_ids": list(result.resource_ids),
                        })
                    errors["base"] = "stale"
                else:
                    errors["base"] = result.flow_error or result.outcome.value
        return self.async_show_form(step_id=step_id, errors=errors, data_schema=probatio.Schema({
            probatio.Required("solcast_api_key"): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD),
            ),
        }))

    async def async_step_clear_solcast(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Only the confirmation step can remove a saved key."""
        if user_input is None:
            return self.async_show_form(step_id="clear_solcast", data_schema=probatio.Schema({}))
        entry = self._get_reconfigure_entry()
        gate = async_get_budget(self.hass, entry.entry_id)
        if not await gate.async_set_first_fetch_pending(False):
            return self.async_show_form(
                step_id="clear_solcast", data_schema=probatio.Schema({}), errors={"base": "stale"},
            )
        data = dict(entry.data)
        data.pop("solcast_api_key", None)
        data.pop("solcast_resource_ids", None)
        return self.async_update_reload_and_abort(entry, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a user-initiated setup to the add-on discovery path."""
        return self.async_abort(reason="addon_required")


class SolcastOptionsFlow(OptionsFlowWithReload):
    """The account's daily call allowance; changed options reload the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose the home account's 10-call or legacy 50-call allowance."""
        if user_input is not None:
            return self.async_create_entry(
                title="", data={**self.config_entry.options, **user_input},
            )
        return self.async_show_form(step_id="init", data_schema=probatio.Schema({
            probatio.Required(
                "calls_limit", default=self.config_entry.options.get("calls_limit", 10),
            ): probatio.In([10, 50]),
        }))
