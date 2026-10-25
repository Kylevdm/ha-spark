"""Supervisor pairing uses the shared discovery contract, never a network probe."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from homeassistant.config_entries import (
    SOURCE_HASSIO,
    SOURCE_USER,
    ConfigEntryState,
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_spark.const import ADDON_SLUG, DOMAIN


def discovery(*, slug: str = ADDON_SLUG, **updates: Any) -> HassioServiceInfo:
    """Read the same config object the add-on posts to Supervisor."""
    config = json.loads(
        (Path(__file__).parents[1] / "tests/fixtures/contract/discovery.json").read_text()
    )
    config.update(updates)
    return HassioServiceInfo(config=config, name="ha-spark", slug=slug, uuid="discovery-uuid")


async def start(hass: HomeAssistant, info: HassioServiceInfo) -> ConfigFlowResult:
    """Start a real hassio discovery flow."""
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=info
    )


@pytest.mark.parametrize("slug", ["other", "ffffffff_ha_spark_addon", "ha_spark_addon"])
async def test_foreign_slug_aborts(hass: HomeAssistant, slug: str) -> None:
    result = await start(hass, discovery(slug=slug))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery"


@pytest.mark.parametrize("config", [None, [], "config", 8099])
async def test_discovery_config_that_is_not_an_object_aborts(
    hass: HomeAssistant, config: Any
) -> None:
    info = HassioServiceInfo(config=config, name="ha-spark", slug=ADDON_SLUG, uuid="d")
    result = await start(hass, info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery"
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("updates", [
    {"host": ""}, {"host": 123}, {"port": True}, {"port": 0}, {"port": 65536},
    {"auth_token": ""}, {"session_id": None},
])
async def test_malformed_discovery_aborts(hass: HomeAssistant, updates: dict[str, Any]) -> None:
    result = await start(hass, discovery(**updates))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery"


async def test_discovery_confirms_without_probe_and_refreshes(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    info = discovery()
    result = await start(hass, info)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    created = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert created["type"] is FlowResultType.CREATE_ENTRY
    entry = created["result"]
    assert str(UUID(entry.unique_id)) == entry.unique_id
    assert entry.unique_id != info.uuid
    assert entry.data == {**info.config, "site_id": entry.unique_id, "slug": ADDON_SLUG}
    await hass.async_block_till_done()
    refresh = await start(
        hass, discovery(host="new-host", port=8100, auth_token="rotated", session_id="new")
    )
    assert refresh["type"] is FlowResultType.ABORT
    assert refresh["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.data["auth_token"] == "rotated"
    assert entry.data["session_id"] == "new"
    assert entry.data["host"] == "new-host"
    assert entry.data["port"] == 8100
    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == entry.data["site_id"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert info.config["auth_token"] not in caplog.text
    assert "rotated" not in caplog.text


async def test_new_discovery_replaces_pending_confirmation(hass: HomeAssistant) -> None:
    old = await start(hass, discovery())
    new = await start(hass, discovery(auth_token="new-token"))
    assert old["flow_id"] not in {f["flow_id"] for f in hass.config_entries.flow.async_progress()}
    result = await hass.config_entries.flow.async_configure(new["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data["auth_token"] == "new-token"


async def test_confirmation_racing_a_refresh_creates_one_entry(hass: HomeAssistant) -> None:
    pending = await start(hass, discovery())
    # Whichever runs first decides, and a refresh may abort the confirmation while its entry
    # is still being set up, so assert the end state, not which caller saw what.
    results = await asyncio.gather(
        hass.config_entries.flow.async_configure(pending["flow_id"], {}),
        start(hass, discovery(auth_token="latest")),
        return_exceptions=True,
    )
    assert all(not isinstance(r, BaseException) or isinstance(r, UnknownFlow) for r in results)
    await hass.async_block_till_done()
    # Confirm whatever is still pending: only the newer discovery's flow may survive.
    for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN):
        await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data["auth_token"] == "latest"


async def test_existing_site_cannot_be_rebound_or_duplicated(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="existing-site", data={"slug": "other"})
    entry.add_to_hass(hass)
    result = await start(hass, discovery())
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == {"slug": "other"}
    user = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert user["type"] is FlowResultType.ABORT
    assert user["reason"] == "addon_required"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_double_confirmation_creates_one_entry(hass: HomeAssistant) -> None:
    pending = await start(hass, discovery())
    await asyncio.gather(
        hass.config_entries.flow.async_configure(pending["flow_id"], {}),
        hass.config_entries.flow.async_configure(pending["flow_id"], {}),
        return_exceptions=True,  # the losing caller may find the flow already removed
    )
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
