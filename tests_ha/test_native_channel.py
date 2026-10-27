"""Native cache snapshots and learned reserved windows at the HTTP seam."""

from datetime import datetime, time, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark import wire
from custom_components.ha_spark.solcast_budget import SolcastBudget
from tests_ha.test_polling import URL as HEARTBEAT
from tests_ha.test_polling import fixture, summary
from tests_ha.test_solcast import KEY, SITE
from tests_ha.test_solcast_forecast import NOW, URL, payload


async def setup_native(hass: HomeAssistant, sites: int = 1) -> MockConfigEntry:
    entry = MockConfigEntry(domain="ha_spark", data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "auth_token": "test", "session_id": fixture("plan_summary")["session_id"],
        "solcast_api_key": KEY, "solcast_resource_ids": [SITE, "second"][:sites],
        "solcast_cache_generation": "test",
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


@pytest.mark.parametrize("name", ["site_snapshot", "site_snapshot_forecast", "site_snapshot_max"])
def test_v2_snapshot_fixtures(name: str) -> None:
    body = fixture(name)
    assert wire.build_snapshot(
        site_id=body["site_id"], release_version=body["release_version"],
        sent_at=datetime.fromisoformat(body["sent_at"]), api_version=2,
        solar_forecast=body["solar_forecast"],
    ) == body
    assert "solar_forecast" not in wire.build_snapshot(
        site_id=body["site_id"], release_version=body["release_version"],
        sent_at=datetime.fromisoformat(body["sent_at"]), api_version=1,
        solar_forecast=body["solar_forecast"],
    )


async def test_cache_crosses_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("UTC")
    aioclient_mock.post(HEARTBEAT, json=summary())
    entry = await setup_native(hass)
    c = entry.runtime_data.solcast
    assert c is not None
    aioclient_mock.get(URL, json=payload())
    await c.async_refresh()
    await entry.runtime_data.async_refresh()
    body = aioclient_mock.mock_calls[-1][2]["solar_forecast"]
    assert body == {"status": "ok", "fetched_at": NOW.isoformat(), "calls_used": 1,
                    "calls_limit": 10, "slots": [[NOW.isoformat(), 1, 2, 3]]}
    assert entry.runtime_data.connected
    await hass.async_block_till_done(wait_background_tasks=True)
    await c.async_shutdown()


@pytest.mark.parametrize("boundary", ["gap", "over_limit"])
async def test_snapshot_truncates_without_clipping(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, boundary: str,
) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("UTC")
    aioclient_mock.post(HEARTBEAT, json=summary())
    entry = await setup_native(hass, sites=2)
    c = entry.runtime_data.solcast
    assert c is not None
    rows = [payload()["forecasts"][0], payload(
        period_end="2026-10-27T13:30:00Z" if boundary == "gap" else "2026-10-27T13:00:00Z",
        pv_estimate90=60 if boundary == "over_limit" else 3,
    )["forecasts"][0]]
    for site in [SITE, "second"]:
        aioclient_mock.get(URL.replace(SITE, site), json={"forecasts": rows})
    await c.async_refresh()
    await entry.runtime_data.async_refresh()
    body = aioclient_mock.mock_calls[-1][2]["solar_forecast"]
    assert body["slots"] == [[NOW.isoformat(), 2, 4, 6]]
    assert len(c.data.periods) == 2  # The cache is neither clipped nor changed by hand-off.
    await hass.async_block_till_done(wait_background_tasks=True)
    await c.async_shutdown()


async def test_learned_window_moves_reserved_poll_and_bad_value_keeps_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW.replace(hour=20))
    await hass.config.async_set_time_zone("UTC")
    # No startup request; the original reservation is 20:40.
    with patch("custom_components.ha_spark.solcast_coordinator.get_astral_event_date",
               return_value=None):
        aioclient_mock.post(HEARTBEAT, json=summary() | {"charge_window_start": None})
        entry = await setup_native(hass)
        c = entry.runtime_data.solcast
        assert c is not None
        learned = summary() | {"charge_window_start": "2026-10-27T22:00:00Z"}
        aioclient_mock.clear_requests()
        aioclient_mock.post(HEARTBEAT, json=learned)
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done(wait_background_tasks=True)
        persisted = await SolcastBudget(hass, c.entry.entry_id).store.async_load()
        assert persisted is not None and persisted["window_start"] == "22:00:00"
        assert c.budget.state is not None and c.budget.state.window_start == time(22)
        for bad in [None, "not-an-instant", "2026-10-27T23:00:00", 123]:
            aioclient_mock.clear_requests()
            aioclient_mock.post(HEARTBEAT, json=summary() | {"charge_window_start": bad})
            await entry.runtime_data.async_refresh()
            await hass.async_block_till_done(wait_background_tasks=True)
            assert entry.runtime_data.connected
            assert c.budget.state is not None and c.budget.state.window_start == time(22)
        aioclient_mock.clear_requests()
        for at, calls in [("2026-10-27T20:40:00Z", 0), ("2026-10-27T21:40:00Z", 1)]:
            instant = datetime.fromisoformat(at)
            freezer.move_to(instant)
            hours = int(((NOW.replace(hour=0) + timedelta(days=2)) - instant)
                        .total_seconds() // 3600) + 1
            # ceil(26h20m) = 27: request fake is deliberately exact.
            aioclient_mock.get(URL.replace("hours=36", f"hours={hours}"), json=payload())
            async_fire_time_changed(hass, instant)
            await hass.async_block_till_done(wait_background_tasks=True)
            forecast_calls = [call for call in aioclient_mock.mock_calls
                              if call[0].lower() == "get"]
            assert len(forecast_calls) == calls
    await c.async_shutdown()


@pytest.mark.parametrize("bad", [None, "bad", "2026-10-24T12:00:00", 123])
def test_window_is_field_local(bad: Any) -> None:
    parsed = wire.parse_summary(fixture("plan_summary") | {"charge_window_start": bad})
    assert parsed.charge_window_start is None
    assert parsed.api_version == 2
    assert wire.parse_summary(fixture("plan_summary_v1")).charge_window_start is None
