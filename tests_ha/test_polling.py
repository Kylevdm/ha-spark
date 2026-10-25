"""Heartbeat liveness is visible through the site's connectivity entity."""

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_HASSIO, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL as YarlURL

from custom_components.ha_spark.const import ADDON_SLUG, DOMAIN
from custom_components.ha_spark.entity import async_clear_unregistered_state
from custom_components.ha_spark.wire import MalformedSummary, parse_summary

FIXTURES = Path(__file__).parents[1] / "tests/fixtures/contract"
URL = "http://addon-test:8099/integration/heartbeat"
TOKEN = "secret-polling-canary"
ENTITY = "binary_sensor.ha_spark_add_on_connected"


def fixture(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{name}.json").read_text()))


def summary() -> dict[str, Any]:
    body = fixture("plan_summary")
    body["loop_tick"] = dt_util.utcnow().isoformat()
    return body


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=fixture("site_snapshot")["site_id"], data={
        "site_id": fixture("site_snapshot")["site_id"], "host": "addon-test", "port": 8099,
        "slug": ADDON_SLUG,
        "auth_token": TOKEN, "session_id": fixture("plan_summary")["session_id"],
    })
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_value(hass: HomeAssistant, entity_id: str = ENTITY) -> str:
    state = hass.states.get(entity_id)
    assert state is not None
    return state.state


def test_summary_fixture() -> None:
    parsed = parse_summary(fixture("plan_summary"))
    assert parsed.api_version == 1
    assert parsed.loop_tick is not None


@pytest.mark.parametrize("updates", [
    {"api_version": True}, {"api_version": 0}, {"built_at": "2026-10-24T12:00:00"},
    {"loop_tick": "2026-10-24T12:00:00"}, {"commands_pending": "false"},
    {"session_id": ""}, {"plan": []}, {"release_version": 3},
    {"built_at": "9999-12-31T23:59:59-01:00"},
])
def test_bad_summary(updates: dict[str, Any]) -> None:
    with pytest.raises(MalformedSummary):
        parse_summary({**fixture("plan_summary"), **updates})


@pytest.mark.parametrize("key", list(fixture("plan_summary")))
def test_missing_summary_key(key: str) -> None:
    body = fixture("plan_summary")
    del body[key]
    with pytest.raises(MalformedSummary):
        parse_summary(body)


async def test_setup_down(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get(ENTITY)
    assert state is not None and state.state == "off"


async def test_snapshot_and_device(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    snapshot = fixture("site_snapshot")
    freezer.move_to(snapshot["sent_at"])
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    assert aioclient_mock.mock_calls[-1][2] == snapshot
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == f"Bearer {TOKEN}"
    state = hass.states.get(ENTITY)
    assert state is not None and state.state == "on"
    assert state.attributes["last_counted_heartbeat"] == snapshot["sent_at"]
    entity = er.async_get(hass).async_get(ENTITY)
    assert entity is not None
    assert entity.unique_id == f"{entry.data['site_id']}_addon_connected"
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.data["site_id"]), entry.entry_id,
    )
    assert device is not None and device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.name == "ha-spark"


@pytest.mark.parametrize("outcome", [
    "unreachable", "timeout", "503", "500", "400", "json", "oversized", "stale", "future",
    "null", "401", "session", "409", "incompatible",
])
async def test_liveness_outcomes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture, outcome: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    aioclient_mock.clear_requests()
    body = summary()
    if outcome in {"unreachable", "timeout"}:
        aioclient_mock.post(URL, exc=aiohttp.ClientError(TOKEN) if outcome == "unreachable"
                            else TimeoutError(TOKEN))
    elif outcome.isdigit():
        aioclient_mock.post(URL, status=int(outcome))
    elif outcome in {"json", "oversized"}:
        aioclient_mock.post(URL, text="invalid" if outcome == "json" else "x" * 65537)
    else:
        if outcome == "stale":
            body["loop_tick"] = (dt_util.utcnow() - timedelta(seconds=121)).isoformat()
        elif outcome == "future":
            body["loop_tick"] = (dt_util.utcnow() + timedelta(seconds=6)).isoformat()
        elif outcome == "null":
            body["loop_tick"] = None
        elif outcome == "session":
            body["session_id"] = "another-session"
        elif outcome == "incompatible":
            body["api_version"] = 2
        aioclient_mock.post(URL, json=body)
    await coordinator.async_refresh()
    expected = {
        "503": "server_error", "500": "server_error", "400": "malformed",
        "json": "malformed", "oversized": "malformed", "stale": "stale_tick",
        "future": "future_tick", "null": "null_tick", "401": "unauthorized",
        "session": "session_mismatch", "409": "version_refused",
    }.get(outcome, outcome)
    assert coordinator.last_outcome == expected
    assert coordinator.last_outcome_at == dt_util.utcnow()
    ended = outcome in {"401", "session"}
    assert state_value(hass) == ("off" if ended else "on")
    assert TOKEN not in caplog.text
    assert TOKEN not in str(coordinator.last_outcome)
    if ended:
        aioclient_mock.clear_requests()
        aioclient_mock.post(URL, json=summary())
        await coordinator.async_refresh()
        assert state_value(hass) == "off"
    # Prevent periodic polls from extending the deadline.
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, status=503)
    freezer.tick(119)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_value(hass) == ("off" if ended else "on")
    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_value(hass) == "off"


async def test_counted_refreshes_deadline(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    freezer.tick(60)
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=summary())
    await entry.runtime_data.async_refresh()
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, status=503)
    freezer.tick(119)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_value(hass) == "on"
    freezer.tick(2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state_value(hass) == "off"


async def test_hung_poll_and_unload_discard_result(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    waiting = asyncio.Event()
    release = asyncio.Event()

    response = AiohttpClientMockResponse("post", YarlURL(URL), json=summary())

    async def hang(_method: str, _url: Any, _kwargs: Any) -> AiohttpClientMockResponse:
        waiting.set()
        await release.wait()
        return response

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=hang)
    poll = asyncio.create_task(coordinator.async_refresh())
    await waiting.wait()
    freezer.tick(121)
    async_fire_time_changed(hass)
    await asyncio.sleep(0)
    assert state_value(hass) == "off"
    assert await hass.config_entries.async_unload(entry.entry_id)
    release.set()
    await poll
    assert not coordinator.connected


@pytest.mark.parametrize("registered", [False, True])
async def test_legacy_cleanup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, registered: bool,
) -> None:
    dropped = ["soc_now", "soc_integrity", "v2l_power_w", "predictions"]
    if registered:
        er.async_get(hass).async_get_or_create("sensor", "other", "registered",
                                             suggested_object_id="ha_spark_soc_now")
    for key in dropped:
        hass.states.async_set(f"sensor.ha_spark_{key}", "legacy")
    aioclient_mock.post(URL, status=503)
    await setup(hass)
    if registered:
        assert state_value(hass, "sensor.ha_spark_soc_now") == "legacy"
    else:
        assert hass.states.get("sensor.ha_spark_soc_now") is None
    for key in dropped[1:]:
        assert hass.states.get(f"sensor.ha_spark_{key}") is None


async def test_helper_frees_entity_id(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    hass.states.async_set(ENTITY, "legacy")
    async_clear_unregistered_state(hass, ENTITY)
    aioclient_mock.post(URL, status=503)
    await setup(hass)
    assert state_value(hass) == "off"
    assert hass.states.get(f"{ENTITY}_2") is None
    assert er.async_get(hass).async_get(ENTITY) is not None


async def test_discovery_discards_old_inflight_reply(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    old = entry.runtime_data
    waiting = asyncio.Event()
    release = asyncio.Event()
    response = AiohttpClientMockResponse("post", YarlURL(URL), json=summary())

    async def hang(_method: str, _url: Any, _data: Any) -> AiohttpClientMockResponse:
        waiting.set()
        await release.wait()
        return response

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=hang)
    poll = asyncio.create_task(old.async_refresh())
    await waiting.wait()
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, status=503)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=HassioServiceInfo(
            config={"host": "addon-test", "port": 8099, "auth_token": "rotated-canary",
                    "session_id": "new-session"},
            name="ha-spark", slug=ADDON_SLUG, uuid="new-discovery",
        ),
    )
    assert result["reason"] == "already_configured"
    # The old HTTP operation is deliberately still pending during the reload.
    await hass.async_block_till_done()
    assert entry.runtime_data is not old
    assert state_value(hass) == "off"
    release.set()
    await poll
    assert not old.connected
    assert state_value(hass) == "off"
    assert TOKEN not in caplog.text
    assert "rotated-canary" not in caplog.text


async def test_summary_arriving_in_parts_counts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, status=503)
    entry = await setup(hass)
    body = json.dumps(summary()).encode()
    protocol = BaseProtocol(asyncio.get_running_loop())
    protocol.connection_made(Mock())  # a waiting read needs a live connection
    stream = StreamReader(protocol, limit=2**16)
    stream.feed_data(body[:10])

    async def rest() -> None:
        await asyncio.sleep(0)
        stream.feed_data(body[10:])
        stream.feed_eof()

    class Partial(AiohttpClientMockResponse):  # type: ignore[misc]
        @property
        def content(self) -> StreamReader:
            return stream

    async def respond(_method: str, _url: Any, _data: Any) -> AiohttpClientMockResponse:
        hass.async_create_task(rest())
        return Partial("post", YarlURL(URL))

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.last_outcome == "counted"
    assert state_value(hass) == "on"
