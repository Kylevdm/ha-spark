"""HTTP security boundary for the companion heartbeat (#476)."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import load_authority
from ha_spark.config import Settings
from ha_spark.energy.scheduler import _ingress_server
from ha_spark.integration_discovery import IntegrationDiscovery

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
CANARY = "CANARY-HEARTBEAT-SECRET"


@pytest.fixture
def discovery() -> IntegrationDiscovery:
    data = json.loads((FIXTURES / "discovery.json").read_text())
    return IntegrationDiscovery(supervisor_token="unused", host=data["host"],
                                auth_token=data["auth_token"], session_id=data["session_id"])


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "ha_spark.db"))
    return AppState(settings, tmp_path / "options.json")


async def request(state: AppState, discovery: IntegrationDiscovery, *, peer: str = "other",
                  method: str = "POST", path: str = "/integration/heartbeat",
                  headers: dict[str, str] | httpx.Headers | None = None,
                  content: bytes | AsyncIterator[bytes] = b"{}") -> httpx.Response:
    app = build_app(state, ingress_peer="proxy", integration=discovery)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1)),
                                 base_url="http://addon") as client:
        return await client.request(method, path, headers=headers, content=content)


@pytest.mark.parametrize("peer", ["proxy", "other"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "bearer x", "Token x", "x"])
async def test_invalid_token(state, discovery, peer, authorization, caplog):
    caplog.set_level(logging.DEBUG)
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await request(state, discovery, peer=peer, headers=headers)
    assert response.status_code == 401
    assert discovery.auth_token not in response.text + caplog.text


async def test_non_ascii_token_refused(state, discovery, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery,
                             headers=httpx.Headers({"Authorization": b"Bearer \xff"}))
    assert response.status_code == 401
    assert discovery.auth_token not in response.text + caplog.text


@pytest.mark.parametrize("header", ["X-Ingress-Path", "X-Hass-Source", "X-Remote-User-Id",
                                     "X-Remote-User-Name", "x-ReMoTe-UsEr-Name"])
async def test_ingress_headers_refused(state, discovery, header, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery, headers={
        "Authorization": f"Bearer {discovery.auth_token}", header: CANARY})
    assert response.status_code == 403
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


@pytest.mark.parametrize("peer", ["proxy", "other"])
async def test_authenticated_peer(state, discovery, peer):
    response = await request(state, discovery, peer=peer,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200


@pytest.mark.parametrize("method,path", [("GET", "/api/mode"), ("POST", "/api/mode"),
    ("GET", "/api/config"), ("GET", "/api/health"), ("GET", "/"),
    ("GET", "/agent/health"), ("POST", "/mcp")])
async def test_token_cannot_open_ingress(state, discovery, method, path):
    response = await request(state, discovery, method=method, path=path,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 403


def test_agent_and_standalone_separation(state, discovery):
    with pytest.raises(ValueError):
        build_app(state, ingress_peer="proxy", integration=discovery, require_token=True)
    with pytest.raises(ValueError):
        build_app(state, integration=discovery)
    with TestClient(build_app(state, require_token=True, token="agent")) as client:
        for path in ("/agent/health", "/mcp"):
            assert client.get(path, headers={
                "Authorization": f"Bearer {discovery.auth_token}"}).status_code == 401
        assert client.post("/integration/heartbeat", headers={
            "Authorization": "Bearer agent"}, json={}).status_code in (401, 404)
    listener = _ingress_server(state, discovery)
    assert listener.config.host == "127.0.0.1"
    with TestClient(listener.config.app) as client:
        assert client.post("/integration/heartbeat", json={}).status_code == 404


@pytest.mark.parametrize("body", [b"not-json-CANARY-HEARTBEAT-SECRET", b"[]",
    b'"CANARY-HEARTBEAT-SECRET"', b"[" * 3000 + b"]" * 3000,
    CANARY.encode() + b"x" * 16385],
    ids=["invalid-json", "array", "string", "deep-nesting", "content-length-cap"])
async def test_invalid_body(state, discovery, body, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery, content=body,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


async def test_chunked_body_cap(state, discovery, caplog):
    caplog.set_level(logging.DEBUG)

    async def chunks() -> AsyncIterator[bytes]:
        yield CANARY.encode() + b"x" * 16384
        raise AssertionError("must stop reading at the cap")

    response = await request(state, discovery, content=chunks(),
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 400
    assert CANARY not in response.text + caplog.text


async def test_integration_namespace(state, discovery):
    headers = {"Authorization": f"Bearer {discovery.auth_token}"}
    assert (await request(state, discovery, method="GET", headers=headers)).status_code == 405
    assert (await request(state, discovery, path="/integration/other",
                          headers=headers)).status_code == 404
    assert (await request(state, discovery, path="/integration/other")).status_code == 401
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/integration/heartbeat"):
                pytest.fail("websocket accepted")
        assert exc.value.code == 1008


@pytest.mark.parametrize("authorization,ingress_header,expected", [
    (False, False, 401), (True, False, 503), (True, True, 403),
])
async def test_repair_auth_order(state, discovery, authorization, ingress_header, expected, caplog):
    caplog.set_level(logging.DEBUG)
    state.settings_service.repair = ["invalid configuration"]
    headers = {"Authorization": f"Bearer {discovery.auth_token}"} if authorization else {}
    if ingress_header:
        headers["X-Ingress-Path"] = CANARY
    response = await request(state, discovery, headers=headers, content=CANARY.encode())
    assert response.status_code == expected
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


async def test_agent_token_cannot_open_integration(tmp_path, discovery, caplog):
    caplog.set_level(logging.DEBUG)
    settings = Settings(ha_url="http://ha.test", ha_token="test", agent_surface="on",
                        agent_api_token=CANARY, db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    response = await request(state, discovery, headers={"Authorization": f"Bearer {CANARY}"})
    assert response.status_code == 401
    assert CANARY not in response.text + caplog.text


async def test_golden_envelope_and_no_mutation(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    before = state.current_options(), state.plan
    authority_before = load_authority(state.settings)
    response = await request(state, discovery, content=b'{"mode":"optimise"}',
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    golden = json.loads((FIXTURES / "plan_summary.json").read_text())
    assert response.json() == golden
    contract = json.loads((FIXTURES / "contract.json").read_text())
    assert golden["api_version"] == contract["api_version"]
    assert (state.current_options(), state.plan) == before
    assert load_authority(state.settings) == authority_before
