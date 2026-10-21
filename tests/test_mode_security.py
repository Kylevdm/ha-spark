"""Mode authority never enters config or agent surfaces."""
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.agent.exposure import OPERATION_TIERS
from ha_spark.agent.mcp_server import build_mcp
from ha_spark.api.server import AppState, build_app
from ha_spark.daemon_channel import Reply, Request
from tests.authority_helpers import seed_authority
from tests.test_owner_mode import settings_at


@pytest.mark.parametrize("body", [
    {}, {"target": "protect"}, {"target": "manual", "expected_epoch": 1},
    {"target": "protect", "expected_epoch": True},
    {"target": "protect", "expected_epoch": "1"},
    {"target": "protect", "expected_epoch": 1, "requester_kind": "cli"},
])
def test_ingress_rejects_bad_mode_body(tmp_path, body):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    with TestClient(build_app(state, ingress_peer="testclient")) as client:
        response = client.post("/api/mode", json=body,
                               headers={"X-Remote-User-Id": "owner", "X-HA-Spark-Request": "mode"})
    assert response.status_code == 400


def test_ingress_rejects_wrong_peer_even_with_spoofed_headers(tmp_path):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    with TestClient(build_app(state, ingress_peer="proxy")) as client:
        response = client.post("/api/mode", json={"target": "protect", "expected_epoch": 1},
                               headers={"X-Remote-User-Id": "owner", "X-Forwarded-For": "proxy"})
    assert response.status_code == 403


@pytest.mark.parametrize("error,status", [("stale_epoch", 409), ("transition_in_progress", 409),
                                         ("preconditions_unmet", 400), (None, 503)])
async def test_ingress_mode_refusal_status(tmp_path, error, status):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    if error:
        state.executor = AsyncMock()
        state.executor.submit_mode.return_value = Reply(error=error)
    app = build_app(state, ingress_peer="proxy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                                base_url="http://local") as client:
        response = await client.post("/api/mode", json={"target": "protect", "expected_epoch": 1},
                                     headers={"X-Remote-User-Id": "owner",
                                              "X-HA-Spark-Request": "mode"})
    assert response.status_code == status


@pytest.mark.parametrize("path", ["/api/config", "/agent/config"])
def test_config_cannot_grant_mode(tmp_path: Path, path: str):
    settings = settings_at(tmp_path).model_copy(update={
        "agent_surface": "on", "agent_exposure": "read_write",
    })
    store = seed_authority(settings, "observe")
    before = store.store_path.read_bytes()
    state = AppState(settings, tmp_path / "options.json", reload=lambda: settings)
    with TestClient(build_app(state)) as client:
        client.post(path, json={"operating_mode": "protect", "committed_mode": "protect",
                                "mode": "protect", "proactive_mode": "on"})
    assert store.store_path.read_bytes() == before


async def test_mode_not_an_agent_operation(tmp_path):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    names = {tool.name for tool in await build_mcp(state).list_tools()}
    assert not any("mode" in name for name in names)
    assert not any("mode" in name for name in OPERATION_TIERS)
    for field in ("requester_id", "requester_kind", "ingress_user"):
        with pytest.raises(ValueError):
            Request.model_validate({"op": "mode", "target": "protect", field: "owner"})


@pytest.mark.parametrize("error", ["stale_epoch", "transition_in_progress", None])
def test_cli_fetches_epoch_and_never_retries(tmp_path, monkeypatch, capsys, error):
    settings = settings_at(tmp_path)
    submit = AsyncMock(side_effect=[Reply(authority={"epoch": 7}),
                                   Reply(error=error, authority={"mode": "protect", "epoch": 8})])
    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    assert cli.main(["mode", "set", "protect"]) == (1 if error else 0)
    assert submit.await_args_list[0].args == (settings, "authority_status")
    assert submit.await_args_list[1].kwargs == {"target": "protect", "expected_epoch": 7}
    assert submit.await_count == 2
    assert (error or "protect") in capsys.readouterr().out


@pytest.mark.parametrize("command", [["mode", "set", "protect"], ["mode", "confirm-observe"]])
def test_cli_mode_without_daemon_refuses(tmp_path, monkeypatch, capsys, command):
    settings = settings_at(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    assert cli.main(command) == 1
    assert "[REFUSED] no ha-spark daemon" in capsys.readouterr().out
