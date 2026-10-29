"""Stored topology and consumed power reports cross the real heartbeat client."""

import json
from pathlib import Path

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import wire
from custom_components.ha_spark.topology import Topology, validate_topology
from tests_ha.test_polling import URL, fixture, summary


@pytest.mark.parametrize("unit,value,expected", [("kW", "2.5", 2.5), ("W", "2500", 2.5),
                                                  ("W", "unavailable", None),
                                                  ("A", "10", None)])
async def test_power_report_crosses_snapshot(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, unit: str, value: str, expected: float | None,
) -> None:
    freezer.move_to("2026-10-24T12:00:00Z")
    raw = json.loads((Path(__file__).parents[1] / "tests/fixtures/contract/site_snapshot_max.json")
                     .read_text())["topology"]["topology"]
    entity = raw["major_loads"][0]["power_entity"]
    hass.states.async_set(entity, value, {"unit_of_measurement": unit})
    state = hass.states.get(entity)
    assert state is not None
    reported = state.last_reported.isoformat()
    aioclient_mock.post(URL, json=summary())
    entry = MockConfigEntry(domain="ha_spark", data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "test", "session_id": fixture("plan_summary")["session_id"],
    }, options={"topology": raw, "topology_revision": 7})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    body = aioclient_mock.mock_calls[-1][2]
    assert body["topology"] == {"configured": True, "revision": 7, "topology": raw}
    assert body["major_loads"][0] == {
        "id": raw["major_loads"][0]["id"], "draw_kw": expected, "reported_at": reported,
    }
    # The first poll's snapshot is built before the guard's startup task runs.
    assert "supply_guard" not in body
    await hass.async_block_till_done(wait_background_tasks=True)
    guard = entry.runtime_data.guard
    assert guard is not None and guard.started
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=summary())
    await entry.runtime_data.async_refresh()
    body = aioclient_mock.mock_calls[-1][2]
    assert body["supply_guard"] == {
        "limiting": False,
        "connections": [{"id": c["id"], "allowance_kw": None} for c in raw["connections"]],
        "restores_left": 48,
        "revision": 7,
    }


def test_maximum_v3_contract() -> None:
    body = fixture("site_snapshot_max")
    assert len(json.dumps(body)) < 32 * 1024
    assert isinstance(validate_topology(body["topology"]["topology"]), Topology)
    parsed = wire.parse_summary(fixture("plan_summary"))
    assert parsed.household_timezone == "Europe/London"
    assert wire.parse_summary(fixture("plan_summary_v2")).household_timezone is None


@pytest.mark.parametrize("bad", [None, "", 4, "secret.with.dot", "x" * 65])
def test_timezone_is_field_local(bad: object) -> None:
    assert wire.parse_summary(fixture("plan_summary") | {"household_timezone": bad})\
        .household_timezone is None
