"""Owner credential flows preserve pairing and expose only fixed errors."""

import logging
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.const import ADDON_SLUG, DOMAIN
from custom_components.ha_spark.diagnostics import async_get_config_entry_diagnostics
from custom_components.ha_spark.solcast import Outcome, SiteResult
from custom_components.ha_spark.solcast_budget import async_get_budget
from tests_ha.test_pairing import discovery, start
from tests_ha.test_solcast import KEY, NOW, SITE, URL


@pytest.fixture(autouse=True)
def freeze_solcast_flows(freezer: FrozenDateTimeFactory) -> None:
    """Credential checks use a deterministic quota day."""
    freezer.move_to(NOW)


def entry_for(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="site", data={
        **discovery().config, "slug": ADDON_SLUG, "site_id": "site", **data,
    })
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(("status", "body", "error"), [
    (200, {"sites": [{"resource_id": SITE}]}, None),
    (401, {}, "auth_failed"), (403, {}, "auth_failed"),
    (200, {"sites": []}, "no_sites"),
    (200, {"sites": [{"resource_id": str(i)} for i in range(3)]}, "too_many_sites"),
    (200, {"sites": [{"resource_id": "bad/id"}]}, "malformed"),
])
async def test_reconfigure_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture, status: int, body: dict[str, Any], error: str | None,
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = entry_for(hass)
    aioclient_mock.get(URL, status=status, json=body)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id,
    })
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"solcast_api_key": KEY},
        )
        await hass.async_block_till_done()
    if error:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": error}
        assert "solcast_api_key" not in entry.data
    else:
        assert result["type"] is FlowResultType.ABORT
        assert entry.data["solcast_api_key"] == KEY
        assert entry.data["solcast_resource_ids"] == [SITE]
        stored = await async_get_budget(hass, entry.entry_id).store.async_load()
        assert stored is not None and stored["first_fetch_pending"] is True
    assert async_get_budget(hass, entry.entry_id).calls_used == 1
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert KEY not in caplog.text + repr(result) + repr(diagnostics)
    assert SITE not in caplog.text + repr(result) + repr(diagnostics)


async def test_clear_requires_confirmation_and_removes_device(hass: HomeAssistant) -> None:
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, "site_solcast"), entry.entry_id)
    site = registry.async_get_device_by_identifier((DOMAIN, "site"), entry.entry_id)
    assert device is not None and site is not None
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.via_device_id == site.id
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id,
    }, data={"solcast_api_key": ""})
    assert result["step_id"] == "clear_solcast"
    assert entry.data["solcast_api_key"] == KEY
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert "solcast_api_key" not in entry.data
    assert not registry.async_get_device_by_identifier((DOMAIN, "site_solcast"), entry.entry_id)


async def test_reauth_replaces_key_and_rediscovery_keeps_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = entry_for(hass, solcast_api_key="old", solcast_resource_ids=["old-site"])
    aioclient_mock.get(URL, json={"sites": [{"resource_id": SITE}]})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": SOURCE_REAUTH, "entry_id": entry.entry_id,
    }, data=dict(entry.data))
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"solcast_api_key": KEY},
        )
        assert result["type"] is FlowResultType.ABORT
        await start(hass, discovery(auth_token="new-token"))
        await hass.async_block_till_done()
    assert entry.data["solcast_api_key"] == KEY
    assert entry.data["solcast_resource_ids"] == [SITE]
    assert entry.data["auth_token"] == "new-token"


@pytest.mark.parametrize("limit", [10, 50])
async def test_options_limit(hass: HomeAssistant, limit: int) -> None:
    entry = entry_for(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"calls_limit": limit},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["calls_limit"] == limit


async def test_solcast_translations(hass: HomeAssistant) -> None:
    translations = await async_get_translations(hass, "en", "config", [DOMAIN])
    for step in ("reconfigure", "reauth_confirm", "clear_solcast"):
        assert translations[f"component.{DOMAIN}.config.step.{step}.title"]
    for error in (
        "auth_failed", "no_sites", "too_many_sites", "malformed", "quota_exhausted",
        "busy", "server_error", "unreachable", "stale",
    ):
        assert translations[f"component.{DOMAIN}.config.error.{error}"]
    options = await async_get_translations(hass, "en", "options", [DOMAIN])
    assert options[f"component.{DOMAIN}.options.step.init.data.calls_limit"]


async def test_loaded_entry_and_flow_share_gate_at_limit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = entry_for(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "site_solcast"), entry.entry_id,
    )
    gate = async_get_budget(hass, entry.entry_id)
    async def counted() -> SiteResult:
        return SiteResult(Outcome.OK)
    for _ in range(10):
        assert (await gate.async_request(counted, calls_limit=10)).outcome == Outcome.OK
    result = await hass.config_entries.flow.async_init(DOMAIN, context={
        "source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id,
    }, data={"solcast_api_key": KEY})
    assert result["errors"] == {"base": "quota_exhausted"}
    assert aioclient_mock.call_count == 0
    assert "solcast_api_key" not in entry.data
