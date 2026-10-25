"""Config flow for the ha-spark companion integration.

Supervisor discovery links one add-on to a generated site identity. Confirmation
never probes the add-on; subsequent boots refresh the connection silently.
Manual host/port/token waits for phase 10.
"""

from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .const import ADDON_SLUG, DOMAIN


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

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a user-initiated setup to the add-on discovery path."""
        return self.async_abort(reason="addon_required")
