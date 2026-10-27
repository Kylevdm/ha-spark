"""Regression coverage for the ticket 04 lifecycle and persistence review."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL as YarlURL

from custom_components.ha_spark.solcast import ForecastResult, Outcome
from custom_components.ha_spark.solcast_coordinator import SolcastCoordinator
from tests_ha.test_solcast import KEY, SITE
from tests_ha.test_solcast import URL as SITES_URL
from tests_ha.test_solcast_flows import entry_for
from tests_ha.test_solcast_forecast import NOW, URL, coordinator, payload


@pytest.fixture(autouse=True)
async def frozen(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("UTC")


async def test_busy_retry_allowance_is_poll_wide(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass)
    second = URL.replace(SITE, "second")
    aioclient_mock.get(URL, side_effect=AsyncMock(side_effect=[
        AiohttpClientMockResponse("get", YarlURL(URL), status=429, json={}),
        AiohttpClientMockResponse("get", YarlURL(URL), json=payload()),
    ]))
    aioclient_mock.get(second, side_effect=AsyncMock(side_effect=[
        AiohttpClientMockResponse("get", YarlURL(second), status=429, json={}),
        AiohttpClientMockResponse("get", YarlURL(second), status=429, json={}),
        AiohttpClientMockResponse("get", YarlURL(second), json=payload()),
    ]))
    with patch("custom_components.ha_spark.solcast_coordinator.asyncio.sleep", new=AsyncMock()):
        await c.async_refresh()
    assert aioclient_mock.call_count == 4
    assert c.budget.calls_used == 4
    assert c.data.status == "stale"
    await c.async_shutdown()


@pytest.mark.parametrize("end", ["0001-01-01T00:00:00Z", "0001-01-01T00:00:00+01:00"])
async def test_timestamp_overflow_keeps_last_good(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, end: str,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, json=payload())
    await c.async_refresh()
    before = c.data.periods[:]
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=payload(period_end=end))
    await c.async_refresh()
    assert c.data.status == "stale"
    assert c.data.periods == before
    await c.async_shutdown()


async def test_swallowed_cache_write_failure_is_stale(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, json=payload())
    with patch.object(c.store, "async_save", new=AsyncMock()):
        await c.async_refresh()
    assert c.data.status == "stale"
    assert c.day_totals(NOW.date()) == (0.5, 1, 1.5)
    await c.async_shutdown()


async def test_auth_latch_restarts_reauth_without_polling(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, status=401)
    await c.async_refresh()
    await c.async_shutdown()
    reloaded = SolcastCoordinator(hass, c.entry)
    with patch.object(c.entry, "async_start_reauth") as reauth:
        await reloaded.async_start()
    reauth.assert_called_once_with(hass)
    assert aioclient_mock.call_count == 1
    await reloaded.async_shutdown()


async def test_cache_generation_discards_auth_latch(hass: HomeAssistant) -> None:
    c = coordinator(hass, 1)
    c.data.status = "auth_failed"
    await c.store.async_save(c.data.model_dump(mode="json"))
    hass.config_entries.async_update_entry(c.entry, data={
        **c.entry.data, "solcast_cache_generation": "new-generation",
    })
    reloaded = SolcastCoordinator(hass, c.entry)
    await reloaded.async_start()
    assert reloaded.data.status == "no_data"
    await reloaded.async_shutdown()
    await c.async_shutdown()


async def test_two_site_aggregate_is_not_clipped(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass)
    for site in (SITE, "second"):
        aioclient_mock.get(URL.replace(SITE, site), json=payload(
            pv_estimate10=80, pv_estimate=90, pv_estimate90=100,
        ))
    await c.async_refresh()
    assert c.data.status == "ok"
    assert c.day_totals(NOW.date()) == (80, 90, 100)
    await c.async_shutdown()
    reloaded = SolcastCoordinator(hass, c.entry)
    await reloaded.async_start()
    assert reloaded.day_totals(NOW.date()) == (80, 90, 100)
    await reloaded.async_shutdown()


async def test_shutdown_awaits_active_poll_and_fences_writes(hass: HomeAssistant) -> None:
    c = coordinator(hass, 1)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def request(*args: Any, **kwargs: Any) -> ForecastResult:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return ForecastResult(Outcome.OK)

    with patch("custom_components.ha_spark.solcast_coordinator.async_fetch_forecast", request):
        task = asyncio.create_task(c.async_refresh())
        await entered.wait()
        await c.async_shutdown()
        assert cancelled.is_set()
        await asyncio.gather(task, return_exceptions=True)
    assert await c.store.async_load() is None


async def test_concurrent_first_fetch_cannot_spend_reserved_calls(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW.replace(hour=20, minute=39))
    c = coordinator(hass)
    await c.budget.store.async_save({
        "utc_day": NOW.date().isoformat(), "calls_used": 7, "exhausted_until": None,
        "window_start": None, "first_fetch_pending": True,
    })
    entered = asyncio.Event()
    release = asyncio.Event()
    owners: list[asyncio.Task[Any] | None] = []

    async def response(method: str, url: YarlURL, data: Any) -> AiohttpClientMockResponse:
        owners.append(asyncio.current_task())
        if len(owners) == 1:
            entered.set()
            await release.wait()
        return AiohttpClientMockResponse(method, url, json=payload())

    for site in (SITE, "second"):
        aioclient_mock.get(URL.replace(SITE, site).replace("hours=36", "hours=28"),
                           side_effect=response)
    await c.async_start()
    await entered.wait()
    first_fetch = owners[0]
    at = NOW.replace(hour=20, minute=40)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    # The ordinary first fetch gets only the unreserved call. Both remaining
    # requests belong to the reserved invocation, even though it queued mid-fetch.
    assert owners.count(first_fetch) == 1
    assert len(owners) == 3
    assert owners[1] == owners[2]
    assert c.budget.calls_used == 10
    assert c.data.status == "ok"
    await c.async_shutdown()


async def test_overdue_reserved_poll_retains_ownership(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW.replace(hour=20, minute=39))
    c = coordinator(hass, 1)
    await c.async_start()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(*args: Any, **kwargs: Any) -> ForecastResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return ForecastResult(Outcome.SERVER_ERROR)

    with patch("custom_components.ha_spark.solcast_coordinator.async_fetch_forecast", request):
        task = asyncio.create_task(c.async_refresh())
        await entered.wait()
        at = NOW.replace(hour=20, minute=40)
        freezer.move_to(at)
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done()
        assert c.budget.state is not None and c.budget.state.reserved_day is None
        release.set()
        await task
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == 2
    assert c.budget.state is not None and c.budget.state.reserved_day == NOW.date()
    await c.async_shutdown()


@pytest.mark.parametrize("source", [SOURCE_REAUTH, SOURCE_RECONFIGURE])
async def test_validated_same_key_rotates_cache_generation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, source: str,
) -> None:
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE],
                      solcast_cache_generation="old-generation")
    c = SolcastCoordinator(hass, entry)
    c.data.status = "auth_failed"
    await c.store.async_save(c.data.model_dump(mode="json"))
    aioclient_mock.get(SITES_URL, json={"sites": [{"resource_id": SITE}]})
    result = await hass.config_entries.flow.async_init("ha_spark", context={
        "source": source, "entry_id": entry.entry_id,
    }, **({"data": dict(entry.data)} if source == SOURCE_REAUTH else {}))
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"solcast_api_key": KEY},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    generation = entry.data["solcast_cache_generation"]
    assert len(generation) == 32 and generation != "old-generation"
    aioclient_mock.get(URL, json=payload())
    reloaded = SolcastCoordinator(hass, entry)
    await reloaded.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert reloaded.data.status == "ok"
    assert reloaded.budget.calls_used == 2
    saved = await reloaded.store.async_load()
    assert saved is not None and saved["generation"] == generation
    assert "identity" not in saved
    await reloaded.async_shutdown()
    await c.async_shutdown()


async def test_clear_removes_cache_generation(hass: HomeAssistant) -> None:
    entry = entry_for(hass, solcast_api_key=KEY, solcast_resource_ids=[SITE],
                      solcast_cache_generation="old-generation")
    result = await hass.config_entries.flow.async_init("ha_spark", context={
        "source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id,
    }, data={"solcast_api_key": ""})
    with patch.object(hass.config_entries, "async_reload", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert "solcast_cache_generation" not in entry.data


async def test_start_does_not_write_cache(hass: HomeAssistant) -> None:
    c = coordinator(hass, 1)
    with patch.object(Store, "async_save", new=AsyncMock()) as save:
        await c.async_start()
    save.assert_not_awaited()
    await c.async_shutdown()


async def test_overrunning_scheduled_poll_still_runs_the_reserved_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory,
) -> None:
    start = NOW.replace(hour=20, minute=39)
    freezer.move_to(start)
    daylight = start.replace(second=50)
    reserved = start.replace(minute=40)

    def schedule(day: Any, **_kwargs: Any) -> list[tuple[Any, bool]]:
        return [(daylight, False), (reserved, True)] if day == start.date() else []

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def request(*args: Any, **kwargs: Any) -> ForecastResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return ForecastResult(Outcome.SERVER_ERROR)

    with (
        patch("custom_components.ha_spark.solcast_coordinator.poll_schedule", schedule),
        patch("custom_components.ha_spark.solcast_coordinator.async_fetch_forecast", request),
    ):
        c = coordinator(hass, 1)
        await c.async_start()
        freezer.move_to(daylight)
        async_fire_time_changed(hass, daylight)
        await entered.wait()
        late = start.replace(minute=41)
        freezer.move_to(late)
        async_fire_time_changed(hass, late)
        await hass.async_block_till_done()
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        async_fire_time_changed(hass, late)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == 2
    assert c.budget.state is not None and c.budget.state.reserved_day == NOW.date()
    await c.async_shutdown()
