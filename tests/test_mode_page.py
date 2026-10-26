"""Ingress mode diagnostics, assets and mutation hardening."""
from hashlib import sha256
from importlib.resources import files
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from ha_spark.api.server import AppState, build_app
from ha_spark.api.static_assets import MANIFEST, asset_names, manifest_text
from ha_spark.authority import authority_view
from ha_spark.daemon_channel import DaemonChannel, Reply
from ha_spark.devices.execution import execution_context
from ha_spark.transitions import PreconditionReport
from tests.authority_helpers import seed_authority, seed_settings
from tests.test_daemon_executor import running_daemon
from tests.test_owner_mode import settings_at

ASSET_PATHS = [
    "/", "/static/index.html", "/static/app.js", "/static/mode.js", "/static/app.css",
    "/static/vendor/htm-3.1.1-preact-standalone.module.js",
    "/static/VENDOR.txt", "/static/MANIFEST.sha256",
    "/static/plan/axis.js", "/static/plan/lanes.js", "/static/plan/soc.js",
    "/static/plan/today.js", "/static/plan/lines.js", "/static/plan/registry.js",
    "/static/plan/carbon.js",
]


@pytest.mark.parametrize("path", [*ASSET_PATHS, "/api/mode"])
@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_page_routes_absent_outside_ingress(tmp_path, path, token, peer):
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    require_token=token, token="TOKEN", ingress_peer=peer)
    with TestClient(app) as client:
        response = client.get(path, headers={"Authorization": "Bearer TOKEN",
                                             "X-Remote-User-Id": "owner"})
    assert response.status_code == 404


@pytest.mark.parametrize("path", ASSET_PATHS)
def test_page_assets_security(tmp_path, path):
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 200
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'self'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cache-Control"] == "no-store"
    if path.endswith(".js"):
        assert response.headers["Content-Type"].startswith("text/javascript")
    elif path.endswith(".css"):
        assert response.headers["Content-Type"].startswith("text/css")
    elif path == "/" or path.endswith(".html"):
        assert response.headers["Content-Type"].startswith("text/html")
        assert 'src="static/app.js"' in response.text
        assert 'href="static/app.css"' in response.text
    with TestClient(build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                              ingress_peer="other")) as client:
        assert client.get(path, headers={
            "X-Forwarded-For": "testclient", "X-Remote-User-Id": "owner",
            "X-Ingress-Path": "/spoofed",
        }).status_code == 403


@pytest.mark.parametrize("path", [
    "/static/", "/static/vendor/", "/static/../server.py", "/static/%2e%2e/server.py",
    "/api/server.py", "/mode.js", "/mode.html", "/missing", "/static/missing.js",
])
def test_shell_has_no_fallback_or_directory_routes(tmp_path, path):
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        assert client.get(path).status_code == 404


def test_packaged_asset_manifest():
    root = files("ha_spark.api").joinpath("static")
    assert root.joinpath(MANIFEST).read_text() == manifest_text(root)
    assert {"/static/" + name for name in asset_names(root)} == set(ASSET_PATHS) - {"/"}
    vendor = root.joinpath("vendor/htm-3.1.1-preact-standalone.module.js").read_bytes()
    assert sha256(vendor).hexdigest() == (
        "72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7"
    )


@pytest.mark.parametrize("mutation", ["added", "changed", "removed"])
def test_manifest_detects_asset_mutations(tmp_path, mutation):
    source = files("ha_spark.api").joinpath("static")
    for name in asset_names(source):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.joinpath(name).read_bytes())
    assert tmp_path.joinpath(MANIFEST).read_text() == manifest_text(tmp_path)
    if mutation == "added":
        (tmp_path / "extra.js").write_text("extra")
    elif mutation == "changed":
        (tmp_path / "app.css").write_text("changed")
    else:
        (tmp_path / "index.html").unlink()
    assert tmp_path.joinpath(MANIFEST).read_text() != manifest_text(tmp_path)


@pytest.mark.parametrize("running", [False, True])
def test_health_reports_daemon_availability(tmp_path, running):
    settings = settings_at(tmp_path)
    store = seed_authority(settings, "observe")
    view = authority_view(store.state, known=True)
    executor = AsyncMock(spec=DaemonChannel) if running else None
    if executor is not None:
        executor.authority_status.return_value = view
    app = build_app(AppState(settings, tmp_path / "options.json", executor=executor),
                    ingress_peer="testclient")
    with TestClient(app) as client:
        data = client.get("/api/health").json()
    assert data["daemon"] == ("running" if running else "unavailable")
    assert data["authority"] == view
    assert data["plan_at"] is None


def test_repair_serves_shell_and_only_repair_apis(tmp_path):
    settings = settings_at(tmp_path)
    executor = DaemonChannel(settings)
    service = seed_settings(tmp_path, {}, settings=settings)
    state = AppState(settings, tmp_path / "options.json", executor=executor,
                     settings_service=service)
    state.settings_service.repair = ["invalid configuration"]
    executor.settings_service = state.settings_service
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        for path in ASSET_PATHS:
            assert client.get(path).status_code == 200
        assert client.get("/api/health").json()["configuration"]["status"] == (
            "configuration_repair"
        )
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/mode").json()["suspended"] == "configuration_repair"
        # The import route is reachable, but still requires owner headers.
        assert client.post("/api/config/import", json={}).status_code == 403
        for method, path in [
            ("POST", "/api/mode"), ("POST", "/api/config"), ("GET", "/api/plan"),
            ("GET", "/api/history"), ("GET", "/api/carbon"), ("POST", "/api/replan"),
            ("POST", "/agent/run"), ("GET", "/openapi.json"), ("GET", "/mode.js"),
        ]:
            response = client.request(method, path, json={}, headers={
                "X-Remote-User-Id": "owner", "X-HA-Spark-Request": "mode",
            })
            assert response.status_code == 503
            assert response.json() == {"error": "configuration_repair"}


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
