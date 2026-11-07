"""Native evidence options and snapshot data remain configuration, not authority."""
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.native_hold import SOURCES
from custom_components.ha_spark.solis_frames import Reading
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_holds import hass_config_dir, held_site  # noqa: F401


async def test_evidence_options_stage_until_whole_document_save(
    held_site: Any,  # noqa: F811
) -> None:
    c = held_site
    old = dict(c.entry.options)
    result = await c.hass.config_entries.options.async_init(c.entry.entry_id)
    assert "evidence" in result["menu_options"]
    flow_id = result["flow_id"]
    result = await c.hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "evidence"})
    assert result["step_id"] == "evidence"
    payload = {"ev_status_entity": "sensor.ev", "battery_direction_source": "native",
               **{f"{key}_max_age_s": 300 for key in SOURCES}}
    result = await c.hass.config_entries.options.async_configure(flow_id, payload)
    assert result["step_id"] == "topology"
    assert dict(c.entry.options) == old
    result = await c.hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["evidence"]["battery_direction"]["source"] == "native"
    await c.hass.async_block_till_done()


@pytest.mark.parametrize("change", [{"battery_direction_max_age_s": 86401},
                                   {"ev_status_max_age_s": 0},
                                   {"battery_direction_source": "entity"}])
async def test_invalid_evidence_options_do_not_save(
    held_site: Any, change: dict[str, object],  # noqa: F811
) -> None:
    c = held_site
    result = await c.hass.config_entries.options.async_init(c.entry.entry_id)
    flow_id = result["flow_id"]
    await c.hass.config_entries.options.async_configure(flow_id, {"next_step_id": "evidence"})
    payload = {"ev_status_entity": "sensor.ev", "battery_direction_source": "native",
               **{f"{key}_max_age_s": 300 for key in SOURCES}, **change}
    payload = {key: value for key, value in payload.items() if value is not None}
    result = await c.hass.config_entries.options.async_configure(flow_id, payload)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]


async def test_native_evidence_uses_bus_not_forged_ha_power_and_never_future_report(
    held_site: Any, native_transport: FakeSolisDevice,  # noqa: F811
) -> None:
    c = held_site
    inv = next(iter(c.native.inverters.values()))
    now = datetime.now(UTC)
    c.hass.states.async_set("sensor.ev", "charging")
    words = [0] * 21
    words[3] = 1  # discharge
    words[17:19] = [0, 2000]
    c.native.readings[inv.id]["input_33132"] = Reading(now, tuple(words))
    c.native.readings[inv.id]["holding_43141"] = Reading(now, (300, 0, 11, 0, 13, 0) + (0,) * 24)
    c.hass.states.async_set("sensor.battery_power", "-9999", {"unit_of_measurement": "W"})
    assert not c.holds.evidence.read(now).overnight
    words[3] = 0
    c.native.readings[inv.id]["input_33132"] = Reading(now, tuple(words))
    assert c.holds.evidence.read(now).overnight
    assert c.holds.evidence.read(now + timedelta(seconds=301)).charging is None
    assert c.holds.evidence.read(now - timedelta(seconds=1)).charging is None


async def test_snapshot_contains_durable_holds_and_native_source(
    held_site: Any, aioclient_mock: AiohttpClientMocker,  # noqa: F811
) -> None:
    c = held_site
    c.hass.states.async_set("sensor.ev", "charging")
    await c.holds.async_reconcile(datetime.now(UTC))
    await c.async_refresh()
    snapshot = next(call[2] for call in reversed(aioclient_mock.mock_calls)
                    if "heartbeat" in str(call[1]))
    assert snapshot["holds"][0]["confirmed"]
    assert snapshot["holds"][0]["device_id"] == "inv1"
    assert snapshot["evidence"]["battery_direction"] == {"source": "native", "max_age_s": 300}
    assert c.control.preconditions().evidence_configured


@pytest.mark.parametrize("check", ["setup", "tick"])
async def test_invalid_evidence_repair_clears_after_options_flow_resave(
    held_site: Any, freezer: Any, check: str,  # noqa: F811
) -> None:
    c = held_site
    entry = c.entry
    registry = ir.async_get(c.hass)
    issue_id = f"evidence_options_invalid_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    c.hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev"}}})
    if check == "setup":
        assert await c.hass.config_entries.async_reload(entry.entry_id)
    else:
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(c.hass, datetime.now(UTC))
    await c.hass.async_block_till_done()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "evidence_options_invalid"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert not issue.translation_placeholders

    result = await c.hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    await c.hass.config_entries.options.async_configure(flow_id, {"next_step_id": "evidence"})
    await c.hass.config_entries.options.async_configure(flow_id, {
        "ev_status_entity": "sensor.ev", "battery_direction_source": "native",
        **{f"{key}_max_age_s": 300 for key in SOURCES}})
    result = await c.hass.config_entries.options.async_configure(flow_id, {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await c.hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert entry.runtime_data.holds.evidence.configured


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}
