"""Bearer-only streamed command routes, without network I/O."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel
from ha_spark.integration_discovery import IntegrationDiscovery


@pytest.fixture
def channel(tmp_path):
    state = AppState(Settings(supervisor_token="unused", db_path=str(tmp_path / "db")),
                     tmp_path / "options.json")
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon",
                                     auth_token="secret", session_id="addon")
    app = build_app(state, ingress_peer="proxy", integration=discovery)
    return state, app


@pytest.mark.parametrize("path", ["fetch", "result"])
async def test_commands_are_bearer_only_and_ingress_is_refused(channel, path):
    _, app = channel
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://addon") as c:
        url = f"/integration/commands/{path}"
        assert (await c.post(url)).status_code == 401
        response = await c.post(url, headers={"Authorization": "Bearer secret",
                                             "X-Remote-User-Id": "CANARY"})
        assert response.status_code == 403
        assert "CANARY" not in response.text
        trailing = await c.post(url + "/", headers={"Authorization": "Bearer secret"})
        assert trailing.status_code == 404
        assert (await c.post(url, headers={"Authorization": "Bearer secret"}, json={})).json() == {
            "error": "invalid command request"}


async def test_fetch_reoffers_and_result_acknowledges(channel):
    state, app = channel
    queue = state.commands
    queue.enqueue(epoch=1, kind="sync_clock", payload={"requester_kind": "cli"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://addon",
                                 headers={"Authorization": "Bearer secret"}) as c:
        payload = {"session_id": "addon", "integration_session_id": "ha", "last_result_sequence": 0}
        first = await c.post("/integration/commands/fetch", json=payload)
        assert first.status_code == 200
        assert len(first.json()["commands"]) == 1
        assert (await c.post("/integration/commands/fetch", json=payload)).json() == first.json()
        result = {"session_id": "addon", "sequence": 1, "outcome": "refused",
                  "reason": "not_available"}
        assert (await c.post("/integration/commands/result", json=result)).status_code == 200
        assert (await c.post("/integration/commands/fetch", json=payload)).json()["commands"] == []


@pytest.mark.parametrize("path,cap", [("fetch", 65536), ("result", 16384)])
async def test_streamed_command_body_caps(channel, path, cap):
    _, app = channel

    async def chunks():
        yield b" " * cap
        yield json.dumps({"secret": "CANARY"}).encode()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://addon") as c:
        response = await c.post(f"/integration/commands/{path}", content=chunks(),
                                headers={"Authorization": "Bearer secret"})
        assert response.status_code == 400
        assert response.json() == {"error": "invalid command request"}


@pytest.mark.parametrize("target", ["protect", "manual", "resume"])
@respx.mock
async def test_native_owner_request_is_queued_once_nudged_and_acknowledged(
    tmp_path, target, monkeypatch,
):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    async with DaemonChannel(settings) as executor:
        state = AppState(settings, tmp_path / "options.json", executor=executor)
        discovery = IntegrationDiscovery(supervisor_token="unused", host="addon",
                                         auth_token="secret", session_id="addon")
        app = build_app(state, ingress_peer="proxy", integration=discovery)
        snapshot = json.loads((Path(__file__).parent / "fixtures/contract/"
                               "site_snapshot_v4_max.json").read_text())
        nudge = asyncio.Event()

        async def prompted(request):
            assert json.loads(request.content) == {}
            nudge.set()
            return httpx.Response(200, json=[])

        service = respx.post(settings.ha_rest_url + "/services/ha_spark/commands_pending")
        service.mock(side_effect=prompted)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("proxy", 1)),
                                     base_url="http://addon") as c:
            bearer = {"Authorization": "Bearer secret"}
            response = await c.post("/integration/heartbeat", json=snapshot, headers=bearer)
            assert response.status_code == 200 and not response.json()["commands_pending"]
            request = asyncio.create_task(c.post("/api/mode", json={
                "target": target, "expected_epoch": 42,
            }, headers={"X-Remote-User-Id": "owner-id", "X-HA-Spark-Request": "mode"}))
            await nudge.wait()
            fetch = {"session_id": "addon", "integration_session_id": "receiver",
                     "last_result_sequence": 0}
            batch = await c.post("/integration/commands/fetch", json=fetch, headers=bearer)
            commands = batch.json()["commands"]
            assert len(commands) == 1
            command = commands[0]
            assert command["epoch"] == 42 and command["payload"]["expected_epoch"] == 42
            assert command["payload"]["user_id"] == "owner-id"
            assert command["kind"] == ("transition" if target == "protect" else "manual")
            result = {"session_id": "addon", "sequence": command["sequence"],
                      "outcome": "applied", "epoch": 43}
            posted = await c.post("/integration/commands/result", json=result, headers=bearer)
            assert posted.status_code == 200
            reply = await request
            assert reply.status_code == 200
            assert reply.json()["command_result"]["outcome"] == "applied"
            assert service.call_count == 1 and state.commands.sequence == 1
            snapshot["authority"]["epoch"] = 43
            response = await c.post("/integration/heartbeat", json=snapshot, headers=bearer)
            assert response.status_code == 200 and not response.json()["commands_pending"]
            shown = await c.get("/api/mode")
            assert shown.json()["epoch"] == 43
            assert shown.json()["command_results"][0]["sequence"] == command["sequence"]


@respx.mock
async def test_native_timeout_reports_pending_at_ten_seconds_without_resubmit(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    async with DaemonChannel(settings) as executor:
        state = AppState(settings, tmp_path / "options.json", executor=executor)
        discovery = IntegrationDiscovery(supervisor_token="unused", host="addon",
                                         auth_token="secret", session_id="addon")
        app = build_app(state, ingress_peer="proxy", integration=discovery)
        snapshot = json.loads((Path(__file__).parent / "fixtures/contract/"
                               "site_snapshot_v4_max.json").read_text())
        respx.post(settings.ha_rest_url + "/services/ha_spark/commands_pending").mock(
            return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("proxy", 1)),
                                     base_url="http://addon") as c:
            bearer = {"Authorization": "Bearer secret"}
            assert (await c.post("/integration/heartbeat", json=snapshot,
                                 headers=bearer)).status_code == 200
            original = asyncio.timeout
            deadlines = []

            def timeout(seconds):
                deadlines.append(seconds)
                return original(0 if seconds == 10 else seconds)

            with monkeypatch.context() as patch:
                patch.setattr(server.asyncio, "timeout", timeout)
                response = await c.post("/api/mode", json={"target": "protect",
                                                         "expected_epoch": 42},
                                        headers={"X-Remote-User-Id": "owner-id",
                                                 "X-HA-Spark-Request": "mode"})
            assert response.status_code == 504 and response.json()["error"] == "pending"
            assert 10 in deadlines
            assert state.commands.sequence == 1 and len(state.commands.pending) == 1
            heartbeat = await c.post("/integration/heartbeat", json=snapshot, headers=bearer)
            assert heartbeat.json()["commands_pending"]


async def test_native_status_keeps_the_health_and_mode_page_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    async with DaemonChannel(settings) as executor:
        state = AppState(settings, tmp_path / "options.json", executor=executor)
        discovery = IntegrationDiscovery(supervisor_token="unused", host="addon",
                                         auth_token="secret", session_id="addon")
        app = build_app(state, ingress_peer="proxy", integration=discovery)
        snapshot = json.loads((Path(__file__).parent / "fixtures/contract/"
                               "site_snapshot_v4_max.json").read_text())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("proxy", 1)),
                                     base_url="http://addon") as c:
            assert (await c.post("/integration/heartbeat", json=snapshot, headers={
                "Authorization": "Bearer secret"})).status_code == 200
            assert state.native_status() is not None
            health = await c.get("/api/health")
            assert health.status_code == 200
            assert health.json()["authority"]["authority_owner"] == "integration"
            assert health.json()["authority"]["save_failed"] is False
            # The mode page refuses a status without preconditions.
            shown = (await c.get("/api/mode")).json()
            assert set(shown["preconditions"]) == {"observe", "protect", "optimise"}
            assert isinstance(shown["holds"], list) and "failing_actions" in shown
