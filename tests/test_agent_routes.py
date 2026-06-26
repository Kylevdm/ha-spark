"""Tests for the agent tool routes (exposure gating + optional token auth)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings


def _state(tmp_path: Path, exposure: str = "read_act") -> AppState:
    options_path = tmp_path / "options.json"
    return AppState(
        settings=Settings(  # type: ignore[call-arg]
            ha_url="http://ha.test",
            ha_token="x",
            db_path=str(tmp_path / "t.db"),
            agent_exposure=exposure,
        ),
        options_path=options_path,
        # rebuild Settings from the persisted file (stands in for load_settings).
        reload=lambda: Settings(**json.loads(options_path.read_text(encoding="utf-8"))),
    )


@respx.mock
def test_read_route_available(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    with TestClient(build_app(_state(tmp_path))) as client:
        assert client.get("/agent/plan").status_code == 200


def test_act_route_absent_in_read_mode(tmp_path: Path) -> None:
    # /agent/run has no GET twin, so its absence is a clean 404 (POST /agent/context
    # would collide with the read GET and return 405 instead).
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        assert client.post("/agent/run", json={}).status_code == 404


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
