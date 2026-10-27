"""Forecast HTTP validation and atomic, durable multi-array polls."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.solcast import Outcome, async_fetch_forecast
from custom_components.ha_spark.solcast_coordinator import SolcastCoordinator
from tests_ha.test_solcast import KEY, SITE

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
URL = f"https://api.solcast.com.au/rooftop_sites/{SITE}/forecasts?format=json&hours=36"


@pytest.fixture(autouse=True)
async def utc_zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


def payload(**changes: Any) -> dict[str, Any]:
    return {"forecasts": [{"period_end": "2026-10-27T12:30:00Z", "period": "PT30M",
                           "pv_estimate10": 1, "pv_estimate": 2, "pv_estimate90": 3,
                           **changes}]}


async def test_forecast_http(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to(NOW)
    aioclient_mock.get(URL, json=payload())
    result = await async_fetch_forecast(async_get_clientsession(hass), KEY, SITE, hours=36)
    assert result.outcome == Outcome.OK
    assert result.periods[0].start == NOW
    assert result.periods[0].p50 == 2
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in caplog.text + repr(result)
    assert SITE not in caplog.text + repr(result)


@pytest.mark.parametrize("changes", [
    {"period": "PT60M"}, {"period_end": "2026-10-27T12:15:00Z"},
    {"period_end": "2026-10-27T12:30:00"}, {"pv_estimate": float("nan")},
    {"pv_estimate10": -1}, {"pv_estimate90": 101}, {"pv_estimate10": 4},
])
async def test_malformed_forecast(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, changes: dict[str, Any],
) -> None:
    aioclient_mock.get(URL, json=payload(**changes))
    result = await async_fetch_forecast(async_get_clientsession(hass), KEY, SITE, hours=36)
    assert result.outcome == Outcome.MALFORMED
    assert result.periods == ()


def coordinator(hass: HomeAssistant, sites: int = 2, limit: int = 10) -> SolcastCoordinator:
    entry = MockConfigEntry(domain="ha_spark", data={
        "solcast_api_key": KEY, "solcast_resource_ids": [SITE, "second"][:sites],
    }, options={"calls_limit": limit})
    entry.add_to_hass(hass)
    return SolcastCoordinator(hass, entry)


async def test_atomic_cache_restart_and_partial_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    c = coordinator(hass)
    aioclient_mock.get(URL, json=payload())
    second = URL.replace(SITE, "second")
    aioclient_mock.get(second, json=payload())
    await c.async_start()
    assert aioclient_mock.call_count == 0
    await c.async_refresh()
    assert c.day_totals(NOW.date()) == (1, 2, 3)
    saved = await c.store.async_load()
    assert saved is not None and saved["status"] == "ok"
    assert KEY not in str(saved) and SITE not in str(saved)
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL, json=payload(pv_estimate=3))
    aioclient_mock.get(second, status=500)
    await c.async_refresh()
    assert c.data.status == "stale"
    assert c.day_totals(NOW.date()) == (1, 2, 3)
    assert c.data.fetched_at == NOW
    await c.async_shutdown()
    reloaded = SolcastCoordinator(hass, c.entry)
    before = aioclient_mock.call_count
    await reloaded.async_start()
    assert aioclient_mock.call_count == before
    assert reloaded.day_totals(NOW.date()) == (1, 2, 3)
    await reloaded.async_shutdown()


async def test_sun_schedule_and_persisted_window(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW.replace(hour=0))
    c = coordinator(hass)
    await c.budget.store.async_save({
        "utc_day": NOW.date().isoformat(), "calls_used": 0, "exhausted_until": None,
        "window_start": "18:00:00", "first_fetch_pending": False,
    })

    def sun(_hass: HomeAssistant, event: str, day: date) -> datetime:
        return datetime(day.year, day.month, day.day, 8 if event == "sunrise" else 16, tzinfo=UTC)

    with patch("custom_components.ha_spark.solcast_coordinator.get_astral_event_date",
               side_effect=sun):
        await c.async_start()
        assert aioclient_mock.call_count == 0
        for i, at in enumerate([NOW.replace(hour=8), NOW, NOW.replace(hour=16),
                                NOW.replace(hour=17, minute=40)]):
            # Ceil(time to midnight two days ahead).
            hours = 48 - at.hour
            for site in (SITE, "second"):
                aioclient_mock.get(URL.replace(SITE, site).replace("hours=36", f"hours={hours}"),
                                   json=payload())
            freezer.move_to(at - timedelta(seconds=1))
            async_fire_time_changed(hass, at - timedelta(seconds=1))
            await hass.async_block_till_done(wait_background_tasks=True)
            assert aioclient_mock.call_count == i * 2
            freezer.move_to(at)
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert aioclient_mock.call_count == (i + 1) * 2
    await c.async_shutdown()


@pytest.mark.parametrize(("sites", "limit", "count"), [(1, 10, 8), (2, 10, 3), (2, 50, 23)])
async def test_daily_schedule_and_repeating_reservation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, sites: int, limit: int, count: int,
) -> None:
    freezer.move_to(NOW.replace(hour=0))
    c = coordinator(hass, sites, limit)
    # UTC local fallback has daylight polls including 07:00 and 19:00.
    with patch("custom_components.ha_spark.solcast_coordinator.get_astral_event_date",
               return_value=None):
        for day in range(2):
            if day == 0:
                await c.async_start()
            else:
                midnight = NOW.replace(hour=0) + timedelta(days=day)
                freezer.move_to(midnight)
                async_fire_time_changed(hass, midnight)
                await hass.async_block_till_done(wait_background_tasks=True)
            for i in range(count):
                at = (NOW.replace(hour=7) + timedelta(days=day)
                      + timedelta(hours=12) * i / (count - 1))
                for site in [SITE, "second"][:sites]:
                    hours = int((NOW.replace(hour=0) + timedelta(days=day + 2) - at)
                                .total_seconds() // 3600)
                    if at.minute or at.second or at.microsecond:
                        hours += 1
                    url = URL.replace(SITE, site).replace("hours=36", f"hours={hours}")
                    aioclient_mock.get(url, json=payload())
                freezer.move_to(at)
                async_fire_time_changed(hass, at)
                await hass.async_block_till_done(wait_background_tasks=True)
            assert aioclient_mock.call_count == (day * (count + 1) + count) * sites
            at = NOW.replace(hour=20, minute=40) + timedelta(days=day)
            for site in [SITE, "second"][:sites]:
                aioclient_mock.get(URL.replace(SITE, site).replace("hours=36", "hours=28"),
                                   json=payload())
            freezer.move_to(at)
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done(wait_background_tasks=True)
            assert aioclient_mock.call_count == (day + 1) * (count + 1) * sites
    await c.async_shutdown()

