"""Native evidence options and snapshot data remain configuration, not authority."""
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native_hold import SOURCES
from custom_components.ha_spark.solis_frames import Reading
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_holds import hass_config_dir, held_site  # noqa: F401
from tests_ha.test_polling import URL, setup, summary


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
               **{f"{key}_max_age_s": 300 for key in SOURCES},
               "ev_liveness_entity": "sensor.ev_live",
               "dispatch_liveness_entity": "sensor.dispatch_live"}
    result = await c.hass.config_entries.options.async_configure(flow_id, payload)
    assert result["step_id"] == "topology"
    assert dict(c.entry.options) == old
    result = await c.hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "save"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["evidence"]["battery_direction"]["source"] == "native"
    assert result["data"]["evidence"]["ev_liveness_entity"] == "sensor.ev_live"
    assert result["data"]["evidence"]["dispatch_liveness_entity"] == "sensor.dispatch_live"
    await c.hass.async_block_till_done()


@pytest.mark.parametrize("change", [{"battery_direction_max_age_s": 86401},
                                   {"ev_status_max_age_s": 0},
                                   {"battery_direction_source": "entity"},
                                   {"ev_liveness_entity": "sensor." + "a" * 256},
                                   {"dispatch_liveness_entity": "sensor." + "a" * 256}])
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


async def test_native_battery_direction_ignores_grid_port_power_sign(
    held_site: Any, native_transport: FakeSolisDevice,  # noqa: F811
) -> None:
    # Observed firmware: s32 battery power, charge negative; grid port import negative.
    c = held_site
    old = next(iter(c.native.inverters.values()))
    inv = old.model_copy(update={"power_sign": "negative_import"})
    c.native.inverters[inv.id] = inv
    now = datetime.now(UTC)
    c.hass.states.async_set("sensor.ev", "charging")
    c.native.readings[inv.id]["holding_43141"] = Reading(now, (300, 0, 11, 0, 13, 0) + (0,) * 24)
    words = [0] * 21
    words[3], words[17], words[18] = 0, 0xFFFF, 0x10000 - 3156  # charging 3156 W
    c.native.readings[inv.id]["input_33132"] = Reading(now, tuple(words))
    assert c.holds.evidence.read(now).overnight
    words[3], words[17], words[18] = 1, 0, 453  # discharging 453 W
    c.native.readings[inv.id]["input_33132"] = Reading(now, tuple(words))
    assert not c.holds.evidence.read(now).overnight


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


@pytest.mark.parametrize("group", ["ev", "dispatch"])
async def test_sibling_evidence_read_and_redacted_diagnostics(
    held_site: Any, freezer: Any, group: str,  # noqa: F811
    aioclient_mock: AiohttpClientMocker,
) -> None:
    from custom_components.ha_spark.diagnostics import async_get_config_entry_diagnostics

    c = held_site
    options = {**c.entry.options["evidence"],
               f"{group}_liveness_entity": "sensor.live",
               "live_dispatch": {"entity_id": "binary_sensor.dispatch", "max_age_s": 123}}
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options,
                                                              "evidence": options})
    now = datetime.now(UTC)
    c.hass.states.async_set("sensor.ev", "charging")
    c.hass.states.async_set("binary_sensor.dispatch", "on", {"planned_dispatches": [{
        "start": now.isoformat(), "end": (now + timedelta(hours=2)).isoformat()}]})
    freezer.tick(timedelta(hours=1))
    c.hass.states.async_set("sensor.live", "alive")
    now = datetime.now(UTC)
    evidence = c.holds.evidence.read(now)
    key = "ev_status" if group == "ev" else "live_dispatch"
    assert c.holds.evidence.freshness(now)[key].via_sibling
    if group == "ev":
        assert evidence.charging is True
        assert evidence.ev_report is None
        assert evidence.ev_via_sibling
    else:
        assert evidence.dispatch_trusted and len(evidence.dispatches) == 1
    diagnostics = await async_get_config_entry_diagnostics(c.hass, c.entry)
    assert diagnostics["evidence"][key] == {
        "validity": "fresh", "age_s": 0.0,
        "max_age_s": 300 if group == "ev" else 123, "via_sibling": True}
    assert "sensor.live" not in str(diagnostics["evidence"])
    await c.async_refresh()
    snapshot = next(call[2] for call in reversed(aioclient_mock.mock_calls)
                    if "heartbeat" in str(call[1]))
    assert snapshot["evidence"][key]["via_sibling"] is True
    assert snapshot["evidence"][key]["validity"] == "fresh"
    assert snapshot["evidence"][key]["age_s"] == 0
    for state in ("unknown", "unavailable"):
        c.hass.states.async_set("sensor.live", state)
        assert c.holds.evidence.freshness(now)[key].validity == "stale"
    c.hass.states.async_set("sensor.live", "alive")
    source_id = "sensor.ev" if group == "ev" else "binary_sensor.dispatch"
    c.hass.states.async_set(source_id, "unavailable")
    assert c.holds.evidence.freshness(now)[key].validity == "unknown"


async def test_siblings_are_limited_to_their_source_groups(
    held_site: Any, freezer: Any,  # noqa: F811
) -> None:
    c = held_site
    options: dict[str, Any] = {
        key: {"entity_id": f"sensor.{key}", "max_age_s": 300} for key in SOURCES}
    options.update(ev_liveness_entity="sensor.ev_live",
                   dispatch_liveness_entity="sensor.dispatch_live")
    c.hass.config_entries.async_update_entry(c.entry, options={**c.entry.options,
                                                              "evidence": options})
    for key in SOURCES:
        c.hass.states.async_set(f"sensor.{key}", "0")
    freezer.tick(timedelta(hours=1))
    c.hass.states.async_set("sensor.ev_live", "alive")
    c.hass.states.async_set("sensor.dispatch_live", "alive")
    result = c.holds.evidence.freshness(datetime.now(UTC))
    for key in SOURCES:
        rescued = key not in ("battery_direction", "dispatch_grid_power")
        assert result[key].via_sibling is rescued
        assert result[key].validity == ("fresh" if rescued else "stale")
    # Fresh liveness cannot turn a missing dispatch list into trusted-empty clearance.
    assert not c.holds.evidence.read(datetime.now(UTC)).dispatch_trusted


async def _advance(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass, datetime.now(UTC))
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("key", ["ev_status", "ev_plug", "ev_power", "live_dispatch",
                                 "adjusted_rate", "dispatch_grid_power"])
async def test_unavailable_evidence_repair_clears_on_fresh_report(
    held_site: SparkCoordinator, freezer: FrozenDateTimeFactory, key: str,  # noqa: F811
) -> None:
    c = held_site
    entry = c.entry
    registry = ir.async_get(c.hass)
    issue_id = f"evidence_source_unavailable_{entry.entry_id}_{key}"
    entity_id = f"sensor.{key}"
    c.hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {
        key: {"entity_id": entity_id, "max_age_s": 300}}})
    c.hass.states.async_set(entity_id, "unavailable")
    await _advance(c.hass, freezer, 30)
    await _advance(c.hass, freezer, 14 * 60 + 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await _advance(c.hass, freezer, 30)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "evidence_source_unavailable"
    assert issue.translation_placeholders == {"source_key": key, "entity_id": entity_id}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert not issue.is_fixable
    for hold in ("ev_hold", "dispatch_hold"):
        state = c.hass.states.get(f"binary_sensor.ha_spark_{hold}")
        assert state is not None and state.state == "off"
    for optional_key in SOURCES:
        if optional_key != key:
            assert registry.async_get_issue(
                DOMAIN, f"evidence_source_unavailable_{entry.entry_id}_{optional_key}") is None

    c.hass.states.async_set(entity_id, "off", {"planned_dispatches": []})
    await _advance(c.hass, freezer, 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_fresh_sibling_breaks_stale_evidence_streak(
    held_site: SparkCoordinator, freezer: FrozenDateTimeFactory,  # noqa: F811
) -> None:
    c = held_site
    entry = c.entry
    registry = ir.async_get(c.hass)
    issue_id = f"evidence_source_unavailable_{entry.entry_id}_live_dispatch"
    c.hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {
        "live_dispatch": {"entity_id": "sensor.dispatch", "max_age_s": 60},
        "dispatch_liveness_entity": "sensor.dispatch_live",
    }})
    c.hass.states.async_set("sensor.dispatch", "off", {"planned_dispatches": []})
    await _advance(c.hass, freezer, 90)
    await _advance(c.hass, freezer, 14 * 60)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    c.hass.states.async_set("sensor.dispatch_live", "alive")
    await _advance(c.hass, freezer, 30)
    await _advance(c.hass, freezer, 60)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await _advance(c.hass, freezer, 15 * 60)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    c.hass.states.async_set("sensor.dispatch_live", "alive")
    await _advance(c.hass, freezer, 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_evidence_repair_restart_does_not_duplicate_and_removal_clears(
    held_site: SparkCoordinator, freezer: FrozenDateTimeFactory,  # noqa: F811
) -> None:
    c = held_site
    entry = c.entry
    registry = ir.async_get(c.hass)
    issue_id = f"evidence_source_unavailable_{entry.entry_id}_ev_status"
    await _advance(c.hass, freezer, 30)
    await _advance(c.hass, freezer, 14 * 60 + 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    assert await c.hass.config_entries.async_reload(entry.entry_id)
    await _advance(c.hass, freezer, 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await _advance(c.hass, freezer, 15 * 60)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    assert await c.hass.config_entries.async_reload(entry.entry_id)
    await _advance(c.hass, freezer, 30)
    await _advance(c.hass, freezer, 15 * 60)
    assert registry.async_get_issue(DOMAIN, issue_id) == issue

    c.hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {}})
    assert await c.hass.config_entries.async_reload(entry.entry_id)
    await _advance(c.hass, freezer, 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_addon_owned_site_clears_evidence_repair_and_does_not_raise_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "evidence": {
        "ev_status": {"entity_id": "sensor.ev", "max_age_s": 300}}})
    issue_id = f"evidence_source_unavailable_{entry.entry_id}_ev_status"
    registry = ir.async_get(hass)
    ir.async_create_issue(
        hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key="evidence_source_unavailable",
        translation_placeholders={"source_key": "ev_status", "entity_id": "sensor.ev"},
    )
    await _advance(hass, freezer, 30)
    assert registry.async_get_issue(DOMAIN, issue_id) is None
    await _advance(hass, freezer, 15 * 60)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}
