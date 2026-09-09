import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str = "read_act", **extra: object) -> AppState:
    options_path = tmp_path / "options.json"
    settings_kw: dict[str, object] = {
        "ha_url": "http://ha.test",
        "ha_token": "x",
        "db_path": str(tmp_path / "t.db"),
        # Master switch on: these tests exercise the exposure tiers; the
        # switch itself is pinned by test_agent_surface_off_*.
        "agent_surface": "on",
        "agent_exposure": exposure,
        **extra,
    }
    return AppState(  # type: ignore[call-arg]
        settings=Settings(**settings_kw),  # type: ignore[arg-type]
        options_path=options_path,
        # reload rebuilds Settings from the persisted options file merged over the
        # original settings_kw, mirroring tests/test_api.py's pattern -- avoids
        # load_settings() falling back to real env/`/data/options.json` creds.
        reload=lambda: Settings(
            **{**settings_kw, **json.loads(options_path.read_text(encoding="utf-8"))}
        ),
    )


@respx.mock
def test_read_route_available(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    with TestClient(build_app(_state(tmp_path))) as client:
        assert client.get("/agent/plan").status_code == 200


def test_act_route_absent_in_read_mode(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        assert client.post("/agent/context", json={}).status_code == 404


def test_config_route_absent_below_read_write(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read_act"))) as client:
        assert client.post("/agent/config", json={"min_soc": 30}).status_code == 404


def test_config_route_present_in_read_write(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read_write"))) as client:
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    assert resp.status_code == 200
    assert resp.json()["min_soc"] == 30.0


@respx.mock
def test_token_required_when_configured(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        assert client.get("/agent/plan").status_code == 401
        ok = client.get("/agent/plan", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code == 200


def test_act_and_write_routes_absent_from_openapi_schema_in_read_mode(tmp_path: Path) -> None:
    """Read-mode must not advertise act/write tools via /openapi.json (design step #7)."""
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "post" not in paths.get("/agent/context", {})
    assert "post" not in paths.get("/agent/run", {})
    assert "post" not in paths.get("/agent/config", {})


def test_read_act_post_context_invalid_body_hits_real_handler_not_404_stub(
    tmp_path: Path,
) -> None:
    """At read_act, POST /agent/context must reach the real add_context handler

    (which 400s on a missing "kind" key), not the read-mode 404 stub and not a
    405 from an unmatched method. This pins that the two branches in
    _agent_router don't both register a route at the same path/method.
    """
    with TestClient(build_app(_state(tmp_path, exposure="read_act"))) as client:
        resp = client.post("/agent/context", json={})
    assert resp.status_code == 400


def test_agent_config_redacts_secrets_in_read_write(tmp_path: Path) -> None:
    """The read_write /agent/config response must not leak secrets in cleartext."""
    state = _state(
        tmp_path,
        exposure="read_write",
        octopus_api_key="SECRET_OCTO",
        agent_api_token="SECRET_AGENT",
    )
    with TestClient(build_app(state)) as client:
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    assert resp.status_code == 200
    assert "SECRET_OCTO" not in resp.text
    assert "SECRET_AGENT" not in resp.text
    body = resp.json()
    assert body["octopus_api_key"] == "***"
    assert body["agent_api_token"] == "***"


@respx.mock
def test_401_response_does_not_leak_token(tmp_path: Path) -> None:
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        resp = client.get("/agent/plan")
    assert resp.status_code == 401
    assert "sekret" not in resp.text


@respx.mock
def test_exposure_downgrade_via_api_config_applies_without_restart(tmp_path: Path) -> None:
    """POST /api/config {"agent_exposure": "read"} shrinks the surface on the same
    client immediately: the gate is evaluated per request against live settings,
    never captured at router-build time (#93)."""
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    with TestClient(build_app(_state(tmp_path, exposure="read_write"))) as client:
        assert client.post("/agent/config", json={"min_soc": 30.0}).status_code == 200
        assert client.post("/api/config", json={"agent_exposure": "read"}).status_code == 200
        # act + write tiers are gone...
        assert client.post("/agent/config", json={}).status_code == 404
        assert client.post("/agent/run").status_code == 404
        assert client.post("/agent/context", json={}).status_code == 404
        # ...read survives, and the schema stops advertising the denied ops.
        assert client.get("/agent/plan").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert "post" not in paths.get("/agent/run", {})
        assert "post" not in paths.get("/agent/config", {})


def test_read_write_advertises_the_full_surface_in_schema(tmp_path: Path) -> None:
    """At read_write the whole /agent/* surface is advertised (pins the schema
    filter against the registered routes -- drift here hides a route)."""
    with TestClient(build_app(_state(tmp_path, exposure="read_write"))) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert {p: set(paths[p]) for p in paths if p.startswith("/agent/")} == {
        "/agent/plan": {"get"},
        "/agent/state": {"get"},
        "/agent/forecast": {"get"},
        "/agent/predictions": {"get"},
        "/agent/health": {"get"},
        "/agent/context": {"get", "post"},
        "/agent/run": {"post"},
        "/agent/config": {"post"},
    }


def test_agent_surface_off_denies_agent_routes_and_schema(tmp_path: Path) -> None:
    """agent_surface="off" (the shipped default) is the master switch: every
    /agent/* route 404s and nothing is advertised in the schema (#93)."""
    with TestClient(build_app(_state(tmp_path, agent_surface="off"))) as client:
        assert client.get("/agent/plan").status_code == 404
        assert client.get("/agent/health").status_code == 404
        assert client.get("/agent/context").status_code == 404
        assert client.post("/agent/context", json={}).status_code == 404
        assert client.post("/agent/run").status_code == 404
        assert client.post("/agent/config", json={}).status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if p.startswith("/agent/")]


def test_exposure_upgrade_via_api_config_applies_without_restart(tmp_path: Path) -> None:
    """The gate is not latched: raising the tier at runtime opens the new routes too."""
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        assert client.post("/agent/config", json={"min_soc": 30.0}).status_code == 404
        assert client.post("/api/config", json={"agent_exposure": "read_write"}).status_code == 200
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    assert resp.status_code == 200
    assert resp.json()["min_soc"] == 30.0
