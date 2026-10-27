"""Energy dashboard solar forecasts from the cached P50 half-hours."""

from datetime import UTC, datetime
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.energy.websocket_api import async_get_energy_platforms
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.energy import async_get_solar_forecast
from tests_ha.test_solcast import KEY, NOW, SITE
from tests_ha.test_solcast_flows import entry_for

FOLD = datetime(2026, 10, 25, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def utc_zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


def forecast(p50_by_period_end: dict[str, float]) -> dict[str, Any]:
    """A validated payload with one period per period end and ordered percentiles."""
    return {"forecasts": [
        {"period_end": period_end, "period": "PT30M",
         "pv_estimate10": value / 2, "pv_estimate": value, "pv_estimate90": value * 2}
        for period_end, value in p50_by_period_end.items()
    ]}


async def loaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    *, hours: int, body: dict[str, Any],
) -> MockConfigEntry:
    """A set-up entry with one completed Solcast poll in its cache."""
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    aioclient_mock.get(
        f"https://api.solcast.com.au/rooftop_sites/{SITE}/forecasts?format=json&hours={hours}",
        json=body,
    )
    await entry.runtime_data.solcast.async_refresh()
    return entry


async def test_hour_sums_and_missing_half_left_out(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    entry = await loaded_entry(hass, aioclient_mock, hours=36, body=forecast({
        "2026-10-27T12:30:00Z": 1, "2026-10-27T13:00:00Z": 2,  # hour 12
        "2026-10-27T13:30:00Z": 4,  # half 13:30 missing, so hour 13 stays out
        "2026-10-27T14:30:00Z": 3, "2026-10-27T15:00:00Z": 5,  # hour 14
    }))
    assert await async_get_solar_forecast(hass, entry.entry_id) == {"wh_hours": {
        "2026-10-27T12:00:00+00:00": 1500.0,
        "2026-10-27T14:00:00+00:00": 4000.0,
    }}


async def test_none_without_entry_key_cache_or_load(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    assert await async_get_solar_forecast(hass, "not-an-entry-id") is None
    bare = entry_for(hass)
    assert await hass.config_entries.async_setup(bare.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await async_get_solar_forecast(hass, bare.entry_id) is None
    keyed = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE])
    assert await hass.config_entries.async_setup(keyed.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await async_get_solar_forecast(hass, keyed.entry_id) is None
    assert await hass.config_entries.async_unload(keyed.entry_id)
    assert await async_get_solar_forecast(hass, keyed.entry_id) is None


async def test_dst_fold_day_keeps_utc_hours(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    # Europe/London folds at 01:00 UTC on 2026-10-25; keys stay UTC hour starts.
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(FOLD)
    entry = await loaded_entry(hass, aioclient_mock, hours=47, body=forecast({
        "2026-10-25T00:30:00Z": 1, "2026-10-25T01:00:00Z": 2,  # hour 00
        "2026-10-25T01:30:00Z": 3, "2026-10-25T02:00:00Z": 4,  # the repeated local hour
    }))
    assert await async_get_solar_forecast(hass, entry.entry_id) == {"wh_hours": {
        "2026-10-25T00:00:00+00:00": 1500.0,
        "2026-10-25T01:00:00+00:00": 3500.0,
    }}


async def test_energy_platform_discovery(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    """HA's Energy component discovers the platform by integration domain."""
    freezer.move_to(NOW)
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await async_get_energy_platforms(hass))[DOMAIN] is async_get_solar_forecast
