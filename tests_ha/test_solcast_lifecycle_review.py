"""Shutdown barriers and startup must not couple Solcast to the heartbeat."""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from tests_ha.test_polling import URL as HEARTBEAT_URL
from tests_ha.test_polling import fixture, summary
from tests_ha.test_solcast import KEY, SITE
from tests_ha.test_solcast_forecast import NOW, URL, coordinator, payload


async def test_setup_heartbeat_does_not_wait_for_solcast_store(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def load(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await release.wait()

    aioclient_mock.post(HEARTBEAT_URL, json=summary())
    entry = MockConfigEntry(domain="ha_spark", data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "token", "session_id": fixture("plan_summary")["session_id"],
        "solcast_api_key": KEY, "solcast_resource_ids": [SITE],
    })
    entry.add_to_hass(hass)
    with patch("custom_components.ha_spark.solcast_coordinator.Store.async_load", load):
        task = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        await entered.wait()
        await hass.async_block_till_done()
        assert task.done()
        assert entry.runtime_data.connected
        release.set()
        assert await task
        await hass.async_block_till_done(wait_background_tasks=True)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("store_kind", ["cache", "quota"])
async def test_shutdown_drains_already_started_store_write(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, store_kind: str,
) -> None:
    freezer.move_to(NOW)
    c = coordinator(hass, 1)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    store = c.store if store_kind == "cache" else c.budget.store
    original = store.async_save

    async def save(data: Any) -> None:
        entered.set()
        await release.wait()
        await original(data)
        finished.set()

    aioclient_mock.get(URL, json=payload())
    with patch.object(store, "async_save", save):
        poll = asyncio.create_task(c.async_refresh())
        await entered.wait()
        shutdown = asyncio.create_task(c.async_shutdown())
        await hass.async_block_till_done()
        assert not shutdown.done()
        release.set()
        await shutdown
        await asyncio.gather(poll, return_exceptions=True)
    assert finished.is_set()
    if store_kind == "quota":
        assert aioclient_mock.call_count == 0
    assert c.data.fetched_at is None or store_kind == "cache"


async def test_stopped_poll_cannot_finish_reservation(hass: HomeAssistant) -> None:
    c = coordinator(hass, 1)
    await c.budget.async_load()
    assert not await c.budget.async_finish_reserved(active=lambda: False)
    assert await c.budget.store.async_load() is None
    await c.async_shutdown()
