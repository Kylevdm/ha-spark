import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str = "read_act") -> AppState:
    options_path = tmp_path / "options.json"
    settings_kw: dict[str, object] = dict(
        ha_url="http://ha.test", ha_token="x",
        db_path=str(tmp_path / "t.db"), agent_exposure=exposure,
    )
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


@respx.mock
def test_401_response_does_not_leak_token(tmp_path: Path) -> None:
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        resp = client.get("/agent/plan")
    assert resp.status_code == 401
    assert "sekret" not in resp.text
