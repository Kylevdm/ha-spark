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
from custom_components.ha_spark.commands import Command, CommandResult, FetchReply, FetchRequest
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.test_native_holds import hass_config_dir, hass_storage, held_site  # noqa: F401
from tests_ha.test_ownership import peer_body
from tests_ha.test_polling import URL, fixture, setup, state_value


@pytest.mark.parametrize("kind", ["transition", "manual", "sync_clock", "reprobe", "apply",
                                  "supervise_export", "end_supervision"])
def test_command_golden(kind: str) -> None:
    body = fixture(f"command_{kind}")
    command = Command.model_validate(body)
    assert command.model_dump() == body
    reply = FetchReply(session_id=command.session_id, commands=[command])
    assert len(reply.model_dump_json().encode()) < 65536


@pytest.mark.parametrize("name", ["command_result", "command_result_apply"])
def test_command_result_and_fetch_golden(name: str) -> None:
    result = fixture(name)
    assert CommandResult.model_validate(result).model_dump() == result
    request = fixture("command_fetch")
    assert FetchRequest.model_validate(request).model_dump() == request


@pytest.mark.parametrize("version", [4, 5])
def test_v4_snapshot_golden(version: int) -> None:
    body = fixture(f"site_snapshot_v{version}")
    assert wire.build_snapshot(
        site_id=body["site_id"], release_version=body["release_version"],
        sent_at=datetime.fromisoformat(body["sent_at"]), api_version=version,
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


def test_v4_summary_export_ack_golden() -> None:
    parsed = wire.parse_summary(fixture("plan_summary_v4_export_acks"))
    assert parsed.export_acks == ("transition_0", "transition_1", "transition_2")


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
        if data["api_version"] >= 4:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal_v3"))
        return AiohttpClientMockResponse("post", YarlURL(URL), json={
            **fixture("plan_summary"), "loop_tick": dt_util.utcnow().isoformat(),
        })

    aioclient_mock.post(URL, side_effect=respond)
    entry = await setup(hass)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert [body["api_version"] for body in bodies] == [7, 3]
    assert bodies[0]["authority_owner"] == "addon"
    assert "authority_owner" not in bodies[1]
    assert state_value(hass) == "on"
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await entry.runtime_data.async_refresh()
    assert [call[2]["api_version"] for call in aioclient_mock.mock_calls] == [3]
    assert state_value(hass) == "on"


async def test_v7_falls_back_to_a_v4_addon(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    # A 0.33 add-on refuses v7; the v4 retry keeps the ownership projection.
    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] >= 5:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal_v4"))
        return AiohttpClientMockResponse("post", YarlURL(URL), json={
            **fixture("plan_summary_v4"), "loop_tick": dt_util.utcnow().isoformat(),
        })

    aioclient_mock.post(URL, side_effect=respond)
    entry = await setup(hass)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert [body["api_version"] for body in bodies] == [7, 4]
    assert "authority_owner" in bodies[1]
    assert entry.runtime_data.api_version == 4


async def test_v6_falls_back_to_a_v5_addon_without_the_handover_flag(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    # A 0.42 add-on refuses v6 (#769); the v5 retry carries no dispatch_handover.
    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] >= 6:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal_v5"))
        return AiohttpClientMockResponse("post", YarlURL(URL), json={
            **fixture("plan_summary_v4"), "api_version": 5,
            "loop_tick": dt_util.utcnow().isoformat(),
        })

    aioclient_mock.post(URL, side_effect=respond)
    entry = await setup(hass)
    bodies = [call[2] for call in aioclient_mock.mock_calls]
    assert [body["api_version"] for body in bodies] == [7, 5]
    assert bodies[0]["dispatch_handover"] is False
    assert "dispatch_handover" not in bodies[1]
    assert entry.runtime_data.api_version == 5


async def test_v7_evidence_options_are_omitted_on_v6_retry(
    held_site: SparkCoordinator,  # noqa: F811 - pytest fixture
    aioclient_mock: AiohttpClientMocker,
) -> None:
    c = held_site
    golden = fixture("site_snapshot_v7")["evidence"]
    c.hass.config_entries.async_update_entry(
        c.entry, options={**c.entry.options, "evidence": golden},
    )

    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] == 7:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal_v6"))
        return AiohttpClientMockResponse("post", YarlURL(URL), json={
            **peer_body(active="retired", version=6), "session_id": c.entry.data["session_id"],
        })

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await c.async_refresh()
    v7, v6 = [call[2] for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert (v7["api_version"], v6["api_version"]) == (7, 6)
    assert v7["authority_owner"] == v6["authority_owner"] == "integration"
    assert c.api_version == 6 and c.connected
    extras = {key: value for key, value in golden.items() if not isinstance(value, dict)}
    assert {key: v7["evidence"][key] for key in extras} == extras
    assert v6["evidence"] == {
        key: value for key, value in v7["evidence"].items() if key not in extras
    }
    for key, value in v6["evidence"].items():
        assert {field: value[field] for field in golden[key]} == golden[key]


def test_v4_version_refusal_golden() -> None:
    parsed = wire.parse_refusal(fixture("version_refusal_v4"))
    assert parsed.supported_api_versions == frozenset({1, 2, 3, 4})
