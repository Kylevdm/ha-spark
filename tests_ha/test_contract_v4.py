"""V4 wire resting values and compatibility through the real heartbeat client."""

from datetime import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL as YarlURL

from custom_components.ha_spark import wire
from tests_ha.test_polling import URL, fixture, setup, state_value


def test_v4_snapshot_golden() -> None:
    body = fixture("site_snapshot_v4")
    assert wire.build_snapshot(
        site_id=body["site_id"], release_version=body["release_version"],
        sent_at=datetime.fromisoformat(body["sent_at"]), api_version=4,
        topology=body["topology"], major_loads=body["major_loads"],
    ) == body
    for version in (1, 2, 3):
        assert "authority_owner" not in wire.build_snapshot(
            site_id=body["site_id"], release_version=body["release_version"],
            sent_at=datetime.fromisoformat(body["sent_at"]), api_version=version,
        )


def test_v4_summary_golden() -> None:
    parsed = wire.parse_summary(fixture("plan_summary_v4"))
    assert parsed.api_version == 4
    assert parsed.addon_authority is not None
    assert parsed.addon_authority.active == "active"
    assert parsed.addon_authority.mode == "authority_unknown"
    assert parsed.addon_authority.store_state == "unknown"
    assert parsed.legacy_overlay == {}
    assert parsed.export_acks == ()
    assert parsed.authority == wire.parse_summary(fixture("plan_summary")).authority


@pytest.mark.parametrize("key", ["addon_authority", "legacy_overlay", "export_acks"])
@pytest.mark.parametrize("value", [None, "SECRET", [], True])
def test_malformed_summary_field_is_unknown(key: str, value: object) -> None:
    body = fixture("plan_summary_v4")
    # An empty list is a valid acknowledgement set, not a malformed block.
    if key == "export_acks" and value == []:
        value = ["bad value"]
    body[key] = value
    parsed = wire.parse_summary(body)
    assert getattr(parsed, key) is None
    assert parsed.authority == wire.parse_summary(fixture("plan_summary")).authority


@pytest.mark.parametrize("key", ["addon_authority", "legacy_overlay", "export_acks"])
def test_missing_summary_field_is_unknown(key: str) -> None:
    body = fixture("plan_summary_v4")
    del body[key]
    assert getattr(wire.parse_summary(body), key) is None


async def test_v4_negotiates_to_030_and_counts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] == 4:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal_v3"))
        return AiohttpClientMockResponse("post", YarlURL(URL), json={
            **fixture("plan_summary"), "loop_tick": dt_util.utcnow().isoformat(),
        })

    aioclient_mock.post(URL, side_effect=respond)
    entry = await setup(hass)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert [body["api_version"] for body in bodies] == [4, 3]
    assert bodies[0]["authority_owner"] == "addon"
    assert "authority_owner" not in bodies[1]
    assert state_value(hass) == "on"
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await entry.runtime_data.async_refresh()
    assert [call[2]["api_version"] for call in aioclient_mock.mock_calls] == [3]
    assert state_value(hass) == "on"


def test_v4_version_refusal_golden() -> None:
    parsed = wire.parse_refusal(fixture("version_refusal_v4"))
    assert parsed.supported_api_versions == frozenset({1, 2, 3, 4})
