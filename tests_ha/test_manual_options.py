"""Future manual deadlines are configurable without moving an agreed deadline."""
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from tests_ha.test_topology_flow import entry_for


async def test_manual_deadline_options_validate_and_preserve_options(hass: HomeAssistant) -> None:
    entry = entry_for(hass, calls_limit=10)
    result: Any = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manual"},
    )
    assert result["type"] is FlowResultType.FORM
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {
            "manual_until_time": "25:00", "household_time_zone": "Europe/London",
        })
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "manual_until_time": "06:30", "household_time_zone": "invalid",
    })
    assert result["errors"] == {"base": "invalid_manual_time"}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "manual_until_time": "06:30", "household_time_zone": "Europe/London",
    })
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["manual_until_time"] == "06:30"
    assert entry.options["household_time_zone"] == "Europe/London"
    assert entry.options["calls_limit"] == 10
