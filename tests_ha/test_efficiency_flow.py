"""The bank form validates and durably carries independent one-way efficiency."""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from tests_ha.test_topology_flow import STORED_TOPOLOGY, choose, entry_for, open_topology


@pytest.mark.parametrize("eta", [0, -0.1, 1.001, float("inf"), float("nan"), "bad"])
async def test_bank_efficiency_refuses_invalid_input(hass: HomeAssistant, eta: Any) -> None:
    entry = entry_for(hass, topology=STORED_TOPOLOGY)
    flow_id = await open_topology(hass, entry)
    await choose(hass, flow_id, "banks")
    await choose(hass, flow_id, "banks_edit")
    await hass.config_entries.options.async_configure(flow_id, {"id": "bank"})
    payload = {**STORED_TOPOLOGY["banks"][0], "one_way_charge_efficiency": eta}
    if isinstance(eta, str):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(flow_id, payload)
    else:
        result = await hass.config_entries.options.async_configure(flow_id, payload)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] is not None
        assert "one_way_charge_efficiency" in result["errors"]
    assert entry.options["topology"] == STORED_TOPOLOGY


@pytest.mark.parametrize("eta", [None, 0.8, 1.0])
async def test_bank_efficiency_saved_with_default(hass: HomeAssistant, eta: float | None) -> None:
    entry = entry_for(hass, topology=STORED_TOPOLOGY)
    flow_id = await open_topology(hass, entry)
    if eta is not None:
        await choose(hass, flow_id, "banks")
        await choose(hass, flow_id, "banks_edit")
        await hass.config_entries.options.async_configure(flow_id, {"id": "bank"})
        result = await hass.config_entries.options.async_configure(
            flow_id, {**STORED_TOPOLOGY["banks"][0], "one_way_charge_efficiency": eta},
        )
        assert result["type"] is FlowResultType.MENU
        await choose(hass, flow_id, "banks_done")
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await choose(hass, flow_id, "save")
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["topology"]["banks"][0]["one_way_charge_efficiency"] == (
        0.949 if eta is None else eta
    )
