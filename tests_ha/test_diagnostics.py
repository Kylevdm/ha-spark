"""Every identifying field leaves the diagnostics download redacted."""

import json
import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.ha_spark.diagnostics import TO_REDACT
from tests_ha.test_solcast import KEY, NOW, SITE
from tests_ha.test_solcast_flows import entry_for

HOST = "canary-host-4d2c"
TOKEN = "canary-token-8e1f"
SESSION = "canary-session-33aa"
SITE_UUID = "canary-uuid-77cd"
CANARIES = (KEY, SITE, HOST, TOKEN, SESSION, SITE_UUID)


@pytest.fixture(autouse=True)
async def utc_zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture(autouse=True)
def freeze_diagnostics(freezer: FrozenDateTimeFactory) -> None:
    """A deterministic fetch age for the Solcast health block."""
    freezer.move_to(NOW)


async def keyed_entry(hass: HomeAssistant) -> MockConfigEntry:
    return entry_for(
        hass, host=HOST, auth_token=TOKEN, session_id=SESSION, site_id=SITE_UUID,
        solcast_api_key=KEY, solcast_resource_ids=[SITE],
    )


async def test_diagnostics_redacts_every_to_redact_field(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = await keyed_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    aioclient_mock.get(
        f"https://api.solcast.com.au/rooftop_sites/{SITE}/forecasts?format=json&hours=36",
        json={"forecasts": [{
            "period_end": "2026-10-27T12:30:00Z", "period": "PT30M",
            "pv_estimate10": 1, "pv_estimate": 2, "pv_estimate90": 3,
        }]},
    )
    await entry.runtime_data.solcast.async_refresh()
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    for field in TO_REDACT & set(entry.data):
        assert data["entry"][field] == "**REDACTED**"
    assert data["solcast"] == {
        "status": "ok", "calls_used": 1, "calls_limit": 10, "fetch_age_s": 0,
        "cached_periods": 1,
    }
    encoded = json.dumps(data)
    for canary in CANARIES:
        assert canary not in encoded
        assert canary not in caplog.text


async def test_diagnostics_without_a_key_has_no_solcast(
    hass: HomeAssistant, hass_client: ClientSessionGenerator,
) -> None:
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert data["solcast"] is None
    assert TOKEN not in json.dumps(data)
