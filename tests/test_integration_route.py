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
SNAPSHOT_BODY = (FIXTURES / "site_snapshot.json").read_bytes()


@pytest.fixture
def discovery() -> IntegrationDiscovery:
    data = json.loads((FIXTURES / "discovery.json").read_text())
    return IntegrationDiscovery(supervisor_token="unused", host=data["host"],
                                auth_token=data["auth_token"], session_id=data["session_id"])


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "ha_spark.db"))
    return AppState(settings, tmp_path / "options.json")


@pytest.fixture
def standalone(tmp_path: Path) -> AppState:
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "ha_spark.db"))
    return AppState(settings, tmp_path / "options.json")


async def request(state: AppState, discovery: IntegrationDiscovery, *, peer: str = "other",
                  method: str = "POST", path: str = "/integration/heartbeat",
                  headers: dict[str, str] | httpx.Headers | None = None,
                  content: bytes | AsyncIterator[bytes] = SNAPSHOT_BODY) -> httpx.Response:
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


def test_agent_and_standalone_separation(state, standalone, discovery):
    with pytest.raises(ValueError):
        build_app(state, ingress_peer="proxy", integration=discovery, require_token=True)
    with pytest.raises(ValueError):
        build_app(state, integration=discovery)
    with pytest.raises(ValueError):
        build_app(standalone, ingress_peer="proxy", integration=discovery)
    with TestClient(build_app(state, require_token=True, token="agent")) as client:
        for path in ("/agent/health", "/mcp"):
            assert client.get(path, headers={
                "Authorization": f"Bearer {discovery.auth_token}"}).status_code == 401
        assert client.post("/integration/heartbeat", headers={
            "Authorization": "Bearer agent"}, json={}).status_code == 404
    listener = _ingress_server(standalone, discovery)
    assert listener.config.host == "127.0.0.1"
    with TestClient(listener.config.app) as client:
        assert client.post("/integration/heartbeat", json={}).status_code == 404


@pytest.mark.parametrize("body", [b"not-json-CANARY-HEARTBEAT-SECRET", b"[]",
    b'"CANARY-HEARTBEAT-SECRET"', b"[" * 3000 + b"]" * 3000,
    CANARY.encode() + b"x" * 16385, b'{"x": NaN}', b'{"x": -Infinity}'],
    ids=["invalid-json", "array", "string", "deep-nesting", "content-length-cap", "nan",
         "infinity"])
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
    trailing = await request(state, discovery, path=f"/integration/heartbeat/?q={CANARY}",
                             headers=headers)
    assert trailing.status_code == 404
    assert CANARY not in str(trailing.headers) + trailing.text
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
    settings = Settings(supervisor_token="test", agent_surface="on",
                        agent_api_token=CANARY, db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    response = await request(state, discovery, headers={"Authorization": f"Bearer {CANARY}"})
    assert response.status_code == 401
    assert CANARY not in response.text + caplog.text


async def test_golden_envelope_and_no_mutation(state, discovery, monkeypatch):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    state.loop_tick = datetime(2026, 10, 24, 11, 59, 30, tzinfo=UTC)
    before = (state.current_options(), state.settings_service.revision,
              state.settings.model_dump(), state.plan, state.plan_at)
    authority_before = load_authority(state.settings)
    response = await request(state, discovery, content=SNAPSHOT_BODY,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    golden = json.loads((FIXTURES / "plan_summary.json").read_text())
    assert response.json() == golden
    contract = json.loads((FIXTURES / "contract.json").read_text())
    assert golden["api_version"] == contract["api_version"]
    assert (state.current_options(), state.settings_service.revision,
            state.settings.model_dump(), state.plan, state.plan_at) == before
    assert load_authority(state.settings) == authority_before


@pytest.mark.parametrize("extra", [{}, {"nested": {"secret": CANARY}, "mode": "optimise"}])
def test_snapshot_accepted_and_unknown_fields_dropped(state, discovery, extra):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", json=snapshot | extra,
                               headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    stored = state.integration_link.snapshot
    assert stored is not None
    known = stored.model_dump()
    assert known == snapshot | {"sent_at": datetime.fromisoformat(snapshot["sent_at"])}
    assert state.integration_link.received_at is not None
    assert state.integration_link.received_at.utcoffset().total_seconds() == 0
    assert not state.integration_link.skew
    assert snapshot["api_version"] == json.loads((FIXTURES / "contract.json").read_text())[
        "api_version"]


@pytest.mark.parametrize("tick", [None, datetime(2026, 10, 24, 11, 50, tzinfo=UTC)])
def test_http_reports_loop_tick_without_refreshing(state, discovery, monkeypatch, tick):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    assert state.loop_tick is None
    state.loop_tick = tick
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        for _ in range(2):
            response = client.post("/integration/heartbeat",
                                   json=json.loads((FIXTURES / "site_snapshot.json").read_text()),
                                   headers={"Authorization": f"Bearer {discovery.auth_token}"})
            assert response.json()["loop_tick"] == (tick.isoformat() if tick else None)
    assert state.loop_tick == tick


@pytest.mark.parametrize("version", [2, 99])
def test_version_skew_keeps_snapshot_and_returns_refusal(state, discovery, version):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        accepted = client.post("/integration/heartbeat", json=snapshot, headers=headers)
        assert accepted.status_code == 200
        previous = state.integration_link
        # No v1 body: a newer version is classified using only the frozen envelope.
        response = client.post("/integration/heartbeat", headers=headers, json={
            "api_version": version, "release_version": "1.0.0", "site_id": [],
            "sent_at": False, "v2": {"new": True}})
    assert response.status_code == 409
    refusal = json.loads((FIXTURES / "version_refusal.json").read_text())
    assert response.json() == refusal
    assert refusal["api_version"] == json.loads((FIXTURES / "contract.json").read_text())[
        "api_version"]
    assert state.integration_link.skew
    assert state.integration_link.snapshot == previous.snapshot
    assert state.integration_link.received_at == previous.received_at


@pytest.mark.parametrize("field,value", [
    ("api_version", None), ("api_version", "1"), ("api_version", True),
    ("api_version", 1.0), ("api_version", 0), ("api_version", -1),
    ("release_version", None), ("release_version", ""), ("release_version", "x" * 65),
    ("release_version", 1), ("site_id", CANARY), ("site_id", None),
    ("site_id", 123), ("sent_at", "2026-10-24T12:00:00"), ("sent_at", 123),
    ("sent_at", None), ("sent_at", "0001-01-01T00:00:00+14:00"),
    ("sent_at", "9999-12-31T23:59:59-14:00"),
])
def test_invalid_snapshot_does_not_mutate_or_echo(state, discovery, caplog, field, value):
    caplog.set_level(logging.DEBUG)
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        accepted = client.post("/integration/heartbeat", json=snapshot, headers=headers)
        assert accepted.status_code == 200
        previous = state.integration_link
        snapshot["canary"] = CANARY
        if value is None:
            del snapshot[field]
        else:
            snapshot[field] = value
        response = client.post("/integration/heartbeat", json=snapshot, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text
    assert state.integration_link is previous


def test_snapshot_normalises_uuid_and_time(state, discovery):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    snapshot["site_id"] = snapshot["site_id"].replace("-", "").upper()
    snapshot["sent_at"] = "2026-10-24T12:59:58+01:00"
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        assert client.post("/integration/heartbeat", json=snapshot, headers={
            "Authorization": f"Bearer {discovery.auth_token}"}).status_code == 200
    stored = state.integration_link.snapshot
    assert stored is not None
    assert stored.site_id == "6f1c2b3a-9d84-4e57-a1c0-2f3e4d5c6b7a"
    assert stored.sent_at == datetime(2026, 10, 24, 11, 59, 58, tzinfo=UTC)
    assert stored.sent_at.tzinfo == UTC
