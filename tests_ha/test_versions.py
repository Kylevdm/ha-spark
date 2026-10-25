"""Version negotiation, repairs and diagnostics through Home Assistant's public seams."""

import json
import logging
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL as YarlURL

from custom_components.ha_spark import wire
from custom_components.ha_spark.const import DOMAIN
from tests_ha.test_polling import TOKEN, URL, fixture, setup, state_value, summary


@pytest.mark.parametrize("supported", [{1}, {1, 2}])
async def test_negotiate_and_keep_version(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, supported: set[int],
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(wire, "SUPPORTED_API_VERSIONS", frozenset(supported))

    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        if data["api_version"] == 2:
            return AiohttpClientMockResponse("post", YarlURL(URL), status=409,
                                            json=fixture("version_refusal"))
        body = summary()
        body["release_version"] = "99.0.0"
        return AiohttpClientMockResponse("post", YarlURL(URL), json=body)

    aioclient_mock.post(URL, side_effect=respond)
    entry = await setup(hass)
    assert state_value(hass) == "on"
    assert [call[2]["api_version"] for call in aioclient_mock.mock_calls] == (
        [2, 1] if 2 in supported else [1]
    )
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, side_effect=respond)
    await entry.runtime_data.async_refresh()
    assert [call[2]["api_version"] for call in aioclient_mock.mock_calls] == [1]
    assert (DOMAIN, "api_version_skew") not in ir.async_get(hass).issues
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("kind", ["refusal", "reverse_refusal", "summary", "retry_refused"])
async def test_skew_ends_session_and_reload_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    initial = summary()
    if kind == "reverse_refusal":
        monkeypatch.setattr(wire, "SUPPORTED_API_VERSIONS", frozenset({2}))
        initial["api_version"] = 2
    aioclient_mock.post(URL, json=initial)
    entry = await setup(hass)
    assert state_value(hass) == "on"
    aioclient_mock.clear_requests()
    body = {"api_version": 2, "release_version": "2.0.0", "supported_api_versions": [2, 3]}
    if kind == "summary":
        # A valid frozen envelope is skew even without a v1 body.
        aioclient_mock.post(URL, json={"api_version": 2, "release_version": "2.0.0"})
    else:
        if kind == "reverse_refusal":
            monkeypatch.setattr(wire, "SUPPORTED_API_VERSIONS", frozenset({2}))
            body = fixture("version_refusal")
        if kind == "retry_refused":
            monkeypatch.setattr(wire, "SUPPORTED_API_VERSIONS", frozenset({1, 2}))
            body["supported_api_versions"] = [1]
        aioclient_mock.post(URL, status=409, json=body)
    await entry.runtime_data.async_refresh()
    assert state_value(hass) == "off"
    assert entry.runtime_data.last_outcome == (
        "incompatible" if kind == "summary" else "version_skew"
    )
    issue = ir.async_get(hass).issues[(DOMAIN, "api_version_skew")]
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "api_version_skew"
    assert issue.translation_placeholders == {
        "addon_release": "0.25.0" if kind == "reverse_refusal" else "2.0.0",
        "addon_api_version": "1" if kind == "reverse_refusal" else "2",
        "integration_api_version": "2" if kind == "reverse_refusal" else "1",
        "supported": ("1, 2" if kind == "retry_refused" else (
            "2" if kind == "reverse_refusal" else "1"
        )),
        "addon_supported": "unknown" if kind == "summary" else (
            "1" if kind in {"retry_refused", "reverse_refusal"} else "2, 3"
        ),
    }
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=summary())
    await entry.runtime_data.async_refresh()
    assert aioclient_mock.call_count == 0
    assert state_value(hass) == "off"
    monkeypatch.setattr(wire, "SUPPORTED_API_VERSIONS", frozenset({1}))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state_value(hass) == "on"
    assert (DOMAIN, "api_version_skew") not in ir.async_get(hass).issues
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("updates", [
    {"release_version": "\u001b[31mhostile"}, {"release_version": "x" * 65},
    {"release_version": 2}, {"supported_api_versions": []},
    {"supported_api_versions": [True]}, {"supported_api_versions": [1, 1]},
    {"supported_api_versions": [0]}, {"supported_api_versions": "1"},
    {"supported_api_versions": list(range(1, 18))}, {"api_version": True},
    {"api_version": 0},
])
async def test_invalid_refusal_is_malformed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture, updates: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG)
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, status=409, json={**fixture("version_refusal"), **updates})
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data.last_outcome == "malformed"
    assert state_value(hass) == "on"
    assert (DOMAIN, "api_version_skew") not in ir.async_get(hass).issues
    assert TOKEN not in caplog.text


async def test_diagnostics_redacts_connection(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    encoded = json.dumps(data)
    assert TOKEN not in encoded
    assert "addon-test" not in encoded
    assert isinstance(data["entry"], dict)
    assert data["entry"]["auth_token"] == "**REDACTED**"
    assert data["entry"]["session_id"] == "**REDACTED**"
    assert entry.data["session_id"] not in encoded
    assert isinstance(data["summary"], dict)
    assert data["summary"]["api_version"] == 1
    assert data["last_outcome"] == "counted"
    assert data["last_outcome_at"] == entry.runtime_data.last_outcome_at.isoformat()
    assert data["integration_api_version"] == 1
    assert data["supported_api_versions"] == [1]
    assert data["addon_api_version"] == 1
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("skew", [False, True])
async def test_diagnostics_before_counted_heartbeat(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator, skew: bool,
) -> None:
    if skew:
        aioclient_mock.post(URL, status=409, json={
            "api_version": 2, "release_version": "2.0.0", "supported_api_versions": [2],
        })
    else:
        aioclient_mock.post(URL, status=503)
    entry = await setup(hass)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert data["summary"] is None
    assert data["last_outcome"] == ("version_skew" if skew else "server_error")
    assert data["addon_api_version"] == (2 if skew else None)
    assert data["integration_api_version"] == 1
    assert data["last_outcome_at"] == entry.runtime_data.last_outcome_at.isoformat()
    assert TOKEN not in json.dumps(data)
    assert "addon-test" not in json.dumps(data)


async def test_diagnostics_after_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    hass_client: ClientSessionGenerator,
) -> None:
    aioclient_mock.post(URL, json=summary())
    entry = await setup(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    data = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert data["summary"] is None
    assert data["last_outcome"] is None
    assert data["entry"]["auth_token"] == "**REDACTED**"
    assert TOKEN not in json.dumps(data)
    assert "addon-test" not in json.dumps(data)


@pytest.mark.parametrize("field", ["release_version", "supported_api_versions"])
def test_refusal_rejects_subclassed_types(field: str) -> None:
    class Str(str):
        pass

    class List(list[int]):
        pass

    body: dict[str, Any] = dict(fixture("version_refusal"))
    body[field] = Str(body[field]) if field == "release_version" else List(body[field])
    with pytest.raises(wire.MalformedSummary):
        wire.parse_refusal(body)
