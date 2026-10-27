"""Cache retention, quota fences and HA's independent Solcast sensor surface."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.solcast import Outcome, SiteResult
from custom_components.ha_spark.solcast_budget import async_get_budget
from custom_components.ha_spark.solcast_coordinator import SolcastCoordinator
from tests_ha.test_polling import URL as HEARTBEAT_URL
from tests_ha.test_polling import fixture, summary
from tests_ha.test_solcast import KEY, SITE
from tests_ha.test_solcast_forecast import NOW, URL, coordinator, payload


@pytest.fixture(autouse=True)
async def frozen_utc(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("UTC")


@pytest.mark.parametrize("status", [200, 500])
async def test_first_fetch_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, status=status, json=payload())
    await c.budget.async_set_first_fetch_pending(True)
    await c.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 1
    await c.async_shutdown()
    reloaded = SolcastCoordinator(hass, c.entry)
    await reloaded.async_start()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 1
    assert reloaded.data.status == ("ok" if status == 200 else "stale")
    await reloaded.async_shutdown()


@pytest.mark.parametrize("body", [payload(period="PT60M"), {"forecasts": []}])
async def test_malformed_poll_keeps_last_good(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: dict[str, Any],
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, json=payload())
    await c.async_refresh()
    before = (await c.store.async_load())
    assert before is not None
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=body)
    await c.async_refresh()
    after = await c.store.async_load()
    assert after is not None and after["status"] == "stale"
    assert after["periods"] == before["periods"]
    assert after["fetched_at"] == before["fetched_at"]
    await c.async_shutdown()


async def test_busy_retries_are_bounded_and_charged(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, status=429, json={})
    with patch("custom_components.ha_spark.solcast_coordinator.asyncio.sleep", new=AsyncMock()):
        await c.async_refresh()
    assert aioclient_mock.call_count == 3
    assert c.budget.calls_used == 3
    assert c.data.status == "stale"
    await c.async_shutdown()


async def test_reserved_cost_is_held_even_for_key_checks(hass: HomeAssistant) -> None:
    c = coordinator(hass)
    gate = async_get_budget(hass, c.entry.entry_id)
    request = AsyncMock(return_value=SiteResult(Outcome.OK))
    for _ in range(8):
        assert (await gate.async_request(request, calls_limit=10)).outcome == "ok"
    assert (await gate.async_request(request, calls_limit=10)).outcome == "quota_exhausted"
    assert request.await_count == 8
    for _ in range(2):
        assert (await gate.async_request(request, calls_limit=10, reserved=True)).outcome == "ok"
    assert request.await_count == 10
    await c.async_shutdown()


async def test_quota_exhaustion_stops_forecast_requests(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, status=429,
                       json={"response_status": {"error_code": "TooManyRequests"}})
    await c.async_refresh()
    await c.async_refresh()
    assert aioclient_mock.call_count == 1
    assert c.data.status == "quota_exhausted"
    freezer.move_to(NOW + timedelta(days=1))
    await c.async_refresh()
    assert aioclient_mock.call_count == 2
    await c.async_shutdown()


@pytest.mark.parametrize(("day", "slots"), [("2026-03-29", 46), ("2026-10-25", 50)])
async def test_dst_day_totals_keep_real_instants(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, day: str, slots: int,
) -> None:
    zone = ZoneInfo("Europe/London")
    midnight = datetime.fromisoformat(day).replace(tzinfo=zone)
    start = midnight.astimezone(UTC)
    end = (midnight + timedelta(days=1)).astimezone(UTC)
    freezer.move_to(start)
    c = coordinator(hass, 1)
    await hass.config.async_set_time_zone("Europe/London")
    periods = []
    at = start
    while at < end:
        p = payload()["forecasts"][0]
        p["period_end"] = (at + timedelta(minutes=30)).isoformat()
        periods.append(p)
        at += timedelta(minutes=30)
    hours = int(((midnight + timedelta(days=2)).astimezone(UTC) - start).total_seconds() / 3600)
    aioclient_mock.get(URL.replace("hours=36", f"hours={hours}"), json={"forecasts": periods})
    await c.async_refresh()
    assert c.data.status == "ok"
    assert len(c.data.periods) == slots
    assert c.day_totals(midnight.date()) == (slots * 0.5, slots, slots * 1.5)
    stored = await c.store.async_load()
    assert stored is not None and len(stored["periods"]) == slots
    await c.async_shutdown()


async def test_past_periods_retained_pruned_and_identity_change_clears(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    c = coordinator(hass, 1)
    aioclient_mock.get(URL, json=payload())
    await c.async_refresh()
    freezer.move_to(NOW + timedelta(minutes=30))
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=payload(period_end="2026-10-27T13:00:00Z"))
    await c.async_refresh()
    assert len(c.data.periods) == 2
    await c.async_shutdown()
    freezer.move_to(NOW + timedelta(days=8))
    reloaded = SolcastCoordinator(hass, c.entry)
    await reloaded.async_start()
    assert reloaded.data.periods == []
    await reloaded.async_shutdown()
    # A new key or site set never reuses a previous owner's periods.
    hass.config_entries.async_update_entry(c.entry, data={
        **c.entry.data, "solcast_api_key": "replacement",
        "solcast_cache_generation": "replacement-generation",
    })
    changed = SolcastCoordinator(hass, c.entry)
    await changed.async_start()
    assert changed.data.fetched_at is None
    assert changed.data.status == "no_data"
    await changed.async_shutdown()


async def test_cache_removal_failure_does_not_block_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(HEARTBEAT_URL, json=summary())
    entry = MockConfigEntry(domain="ha_spark", data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "token", "session_id": fixture("plan_summary")["session_id"],
    })
    entry.add_to_hass(hass)
    with patch("custom_components.ha_spark.__init__.Store.async_remove", side_effect=OSError()):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.connected
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unload_cancels_reserved_timer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW.replace(hour=20))
    c = coordinator(hass, 1)
    await c.async_start()
    await c.async_shutdown()
    at = NOW.replace(hour=20, minute=40)
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert aioclient_mock.call_count == 0


async def test_corrupt_cache_loads_empty_without_polling(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    c = coordinator(hass, 1)
    await c.store.async_save({"periods": [{"start": "not-an-instant"}], "status": "ok"})
    await c.async_start()
    assert c.data.periods == []
    assert c.day_totals(NOW.date()) is None
    assert c.data.fetched_at is None
    assert aioclient_mock.call_count == 0
    await c.async_shutdown()


async def test_ha_sensors_auth_failure_and_heartbeat_independence(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    aioclient_mock.post(HEARTBEAT_URL, json=summary())
    entry = MockConfigEntry(domain="ha_spark", unique_id=fixture("site_snapshot")["site_id"], data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "token", "session_id": fixture("plan_summary")["session_id"],
        "solcast_api_key": KEY, "solcast_resource_ids": [SITE],
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    def state(key: str) -> Any:
        entity_id = registry.async_get_entity_id("sensor", "ha_spark",
                                                 f"{entry.data['site_id']}_solcast_{key}")
        assert entity_id is not None
        return hass.states.get(entity_id)
    assert state("forecast_today").state == "unavailable"
    assert state("forecast_tomorrow").state == "unavailable"
    c = entry.runtime_data.solcast
    assert c is not None
    aioclient_mock.get(URL, json={"forecasts": [
        *payload()["forecasts"],
        *payload(period_end="2026-10-28T12:30:00Z")["forecasts"],
    ]})
    await c.async_refresh()
    tomorrow = state("forecast_tomorrow")
    assert tomorrow.state == "1.0"
    assert tomorrow.attributes["estimate10"] == 0.5
    assert tomorrow.attributes["estimate90"] == 1.5
    today = state("forecast_today")
    assert today.state == "1.0"
    assert today.attributes["estimate10"] == 0.5
    assert today.attributes["estimate90"] == 1.5
    assert "slots" not in today.attributes
    assert state("last_fetch").state == NOW.isoformat()
    assert state("api_calls_used").state == "1"
    assert state("api_calls_used").attributes["limit"] == 10
    assert not ir.async_get(hass).issues
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, status=401, text=KEY + SITE)
    await c.async_refresh()
    await hass.async_block_till_done()
    assert any(flow["context"]["source"] == "reauth"
               for flow in hass.config_entries.flow.async_progress())
    calls = aioclient_mock.call_count
    await c.async_refresh()
    assert aioclient_mock.call_count == calls
    assert entry.runtime_data.connected
    assert KEY not in caplog.text and SITE not in caplog.text
    await hass.config_entries.async_unload(entry.entry_id)
