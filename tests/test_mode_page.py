"""Ingress mode diagnostics, assets and mutation hardening."""
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from ha_spark.api.server import AppState, build_app
from ha_spark.authority import authority_view
from ha_spark.daemon_channel import DaemonChannel, Reply
from ha_spark.devices.execution import execution_context
from ha_spark.transitions import PreconditionReport
from tests.authority_helpers import seed_authority
from tests.test_daemon_executor import running_daemon
from tests.test_owner_mode import settings_at


@pytest.mark.parametrize("path", ["/", "/mode.js", "/api/mode"])
@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_page_routes_absent_outside_ingress(tmp_path, path, token, peer):
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    require_token=token, token="TOKEN", ingress_peer=peer)
    with TestClient(app) as client:
        response = client.get(path, headers={"Authorization": "Bearer TOKEN",
                                             "X-Remote-User-Id": "owner"})
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/", "/mode.js"])
def test_page_assets_security(tmp_path, path):
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self'; frame-ancestors 'self'; "
        "base-uri 'none'; form-action 'none'"
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    if path.endswith(".js"):
        for forbidden in ("innerHTML", "eval", "new Function", "/api/"):
            assert forbidden not in response.text
        assert "expected_epoch" in response.text
        assert "supervised" in response.text
        assert "disable any pre-existing automations or manual schedules" in response.text
    else:
        assert 'src="mode.js"' in response.text
    with TestClient(build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                              ingress_peer="other")) as client:
        assert client.get(path).status_code == 403


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise", "authority_unknown"])
@pytest.mark.parametrize("target", [None, "observe", "protect", "optimise"])
@respx.mock
def test_mode_status_includes_authority_and_preconditions(tmp_path, mode, target):
    settings = settings_at(tmp_path)
    store = seed_authority(settings, "observe" if mode == "authority_unknown" else mode)
    view = authority_view(store.state, known=mode != "authority_unknown")
    view.update(transition={"target": target, "failing_actions": ["action failed"]}
                if target else None,
                failing_actions=["action failed"] if target else [], holds=[{"cause": "ev"}],
                fallback={"active": True, "causes": ["soc_integrity"]})
    executor = AsyncMock()
    executor.authority_status = lambda: view.copy()
    respx.get(url__regex=r"http://ha.test/.*").mock(return_value=Response(404))
    before = store.store_path.read_bytes()
    app = build_app(AppState(settings, tmp_path / "options.json", executor=executor),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get("/api/mode")
    assert response.status_code == 200
    data = response.json()
    for key, value in view.items():
        assert data[key] == value
    assert data["preconditions"]["observe"] == {"passed": True, "reasons": []}
    assert not data["preconditions"]["protect"]["passed"]
    assert len(data["preconditions"]["optimise"]["reasons"]) >= 2
    assert store.store_path.read_bytes() == before


@pytest.mark.parametrize("headers,body,status", [
    ({"Content-Type": "application/json"}, '{}', 403),
    ({"X-HA-Spark-Request": "mode"}, '{}', 415),
    ({"X-HA-Spark-Request": "mode", "Content-Type": "text/plain"}, '{}', 415),
])
def test_mode_post_requires_csrf_headers(tmp_path, headers, body, status):
    executor = AsyncMock(spec=DaemonChannel)
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json", executor=executor),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/mode", content=body,
                               headers={"X-Remote-User-Id": "owner", **headers})
    assert response.status_code == status
    executor.submit_mode.assert_not_called()


@pytest.mark.parametrize("error,status", [("preconditions_unmet", 400), ("stale_epoch", 409)])
def test_mode_post_returns_all_reasons(tmp_path, error, status):
    executor = AsyncMock(spec=DaemonChannel)
    executor.submit_mode.return_value = Reply(error=error, text="first; second",
                                             reasons=["first", "second"])
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json", executor=executor),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/mode", json={"target": "optimise", "expected_epoch": 7},
                               headers={"X-Remote-User-Id": "owner", "X-HA-Spark-Request": "mode"})
    assert response.status_code == status
    assert response.json() == {"error": error, "detail": "first; second",
                               "reasons": ["first", "second"]}
    assert executor.submit_mode.call_count == 1


@respx.mock
async def test_daemon_refusal_preserves_every_precondition(tmp_path, monkeypatch):
    settings = settings_at(tmp_path)
    seed_authority(settings, "observe")
    reasons = ("model is not validated", "evidence age is missing")
    monkeypatch.setattr("ha_spark.energy.scheduler.check_preconditions",
                        AsyncMock(return_value=PreconditionReport(protect=reasons,
                                                                  optimise=reasons)))
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    prior = execution_context().authority
    try:
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, _, device, _):
            epoch = state.executor.authority_status()["epoch"]
            reply = await state.executor.submit_mode("protect", epoch, ingress_user="owner")
            assert reply.error == "preconditions_unmet"
            assert reply.reasons == list(reasons)
            assert reply.text == "; ".join(reasons)
            assert state.executor.authority_status()["epoch"] == epoch
            device.release_holds.assert_not_awaited()
    finally:
        execution_context().authority = prior
