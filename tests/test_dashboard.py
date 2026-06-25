"""Tests for Lovelace dashboard generation from configured Settings."""

from __future__ import annotations

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.dashboard import build_dashboard
from ha_spark.ha.rest import HomeAssistantRest


def _states_json() -> list[dict[str, object]]:
    return [
        {
            "entity_id": "sensor.solisac_battery_soc",
            "state": "82",
            "attributes": {"friendly_name": "Battery SoC (Live)"},
        },
        {
            "entity_id": "sensor.solcast_forecast",
            "state": "12.3",
            "attributes": {"friendly_name": "Solar Forecast Tomorrow"},
        },
    ]


@respx.mock
async def test_build_dashboard_groups_configured_fields_with_live_names() -> None:
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=_states_json())
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        soc_entity="sensor.solisac_battery_soc",
        solar_tomorrow_entity="sensor.solcast_forecast",
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    cards = dashboard["views"][0]["cards"]
    titles = [c["title"] for c in cards]
    assert "Battery" in titles
    assert "Solar" in titles

    battery = next(c for c in cards if c["title"] == "Battery")
    assert battery["entities"] == [
        {"entity": "sensor.solisac_battery_soc", "name": "Battery SoC (Live)"}
    ]
    solar = next(c for c in cards if c["title"] == "Solar")
    assert solar["entities"] == [
        {"entity": "sensor.solcast_forecast", "name": "Solar Forecast Tomorrow"}
    ]


@respx.mock
async def test_build_dashboard_skips_unconfigured_categories() -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    cards = dashboard["views"][0]["cards"]
    titles = [c["title"] for c in cards]
    # outdoor_weather_entity defaults to weather.home, so "Other" is always present.
    assert titles == ["Battery", "Other"]


@respx.mock
async def test_build_dashboard_falls_back_to_static_labels_when_ha_unreachable() -> None:
    respx.get("http://ha.test/api/states").mock(side_effect=httpx.ConnectError("refused"))
    settings = Settings(ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    battery = next(
        c for c in dashboard["views"][0]["cards"] if c["title"] == "Battery"
    )
    assert battery["entities"] == [{"entity": "sensor.soc", "name": "Battery SoC"}]


@respx.mock
async def test_build_dashboard_adds_people_card_from_csv_field() -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="t",
        person_entities="person.alice, person.bob",
    )
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        dashboard = await build_dashboard(settings, rest)

    people = next(c for c in dashboard["views"][0]["cards"] if c["title"] == "People")
    assert people["entities"] == [
        {"entity": "person.alice", "name": "person.alice"},
        {"entity": "person.bob", "name": "person.bob"},
    ]
