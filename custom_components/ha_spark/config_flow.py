"""Config flow for the ha-spark companion integration.

The only step this phase offers is a user-initiated setup, and it points the
owner at the ha-spark add-on: the entry is created from the Supervisor
discovery the add-on announces once it is serving, never from hand-typed
connection data (manual host/port/token waits for phase 10).
"""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class HaSparkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ha-spark config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Send a user-initiated setup to the add-on discovery path."""
        return self.async_abort(reason="addon_required")
