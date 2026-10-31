"""The integration loads in Home Assistant, and a user flow aborts (#473)."""

from pathlib import Path

import homeassistant
from homeassistant.config_entries import SOURCE_USER, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from custom_components.ha_spark.const import DOMAIN


async def test_manifest_loads_as_a_bare_custom_integration(hass: HomeAssistant) -> None:
    """The skeleton is loadable; its only pip requirement is the pymodbus core pins."""
    integration = await async_get_integration(hass, DOMAIN)
    assert integration.domain == DOMAIN
    assert integration.manifest["config_flow"] is True
    assert integration.manifest["requirements"] == ["pymodbus==3.13.1"]
    constraints = Path(homeassistant.__file__).parent / "package_constraints.txt"
    assert "pymodbus==3.13.1" in constraints.read_text(encoding="utf-8").splitlines()
    assert "single_config_entry" not in integration.manifest
    assert await async_setup_component(hass, DOMAIN, {})
    assert DOMAIN in hass.config.components


async def test_user_flow_aborts_at_the_addon(hass: HomeAssistant) -> None:
    """A setup the user starts points at the add-on instead of asking for data."""
    result: ConfigFlowResult = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "addon_required"
    translations = await async_get_translations(hass, "en", "config", [DOMAIN])
    message = translations[f"component.{DOMAIN}.config.abort.addon_required"]
    assert "Install and start the ha-spark add-on" in message
    assert "discovers" in message
