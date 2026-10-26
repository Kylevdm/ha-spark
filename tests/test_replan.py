"""The ingress Replan route: the CLI's replan path, gated like every owner write."""
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply, action_lines
from tests.authority_helpers import seed_authority, seed_settings
from tests.test_daemon_executor import running_daemon


def settings_at(path: Path) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="SECRET", db_path=str(path / "t.db"))


def ingress_state(tmp_path: Path, executor: DaemonChannel | None = None) -> AppState:
    return AppState(settings_at(tmp_path), tmp_path / "options.json", executor=executor)


@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_replan_route_absent_outside_ingress(tmp_path: Path, token: bool, peer: str | None) -> None:
    app = build_app(ingress_state(tmp_path), require_token=token, token="TOKEN", ingress_peer=peer)
    with TestClient(app) as client:
        response = client.post("/api/replan", json={}, headers={
            "Authorization": "Bearer TOKEN", "X-Remote-User-Id": "owner",
            "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == 404


@pytest.mark.parametrize("user", [None, "", "name with spaces", "a\nforged", "a" * 65])
def test_replan_requires_a_valid_user_id(tmp_path: Path, user: str | None) -> None:
    executor = AsyncMock(spec=DaemonChannel)
    app = build_app(ingress_state(tmp_path, executor), ingress_peer="testclient")
    # The custom header is valid, so the user id is the only reason to refuse.
    headers = {"X-HA-Spark-Request": "replan"}
    if user is not None:
        headers["X-Remote-User-Id"] = user
    with TestClient(app) as client:
        response = client.post("/api/replan", json={}, headers=headers)
    assert response.status_code == 403
    executor.submit.assert_not_called()


@pytest.mark.parametrize("headers,status", [
    ({}, 403),
    ({"X-HA-Spark-Request": "mode"}, 403),
    ({"X-HA-Spark-Request": "replan"}, 415),
    ({"X-HA-Spark-Request": "replan", "Content-Type": "text/plain"}, 415),
])
def test_replan_requires_the_custom_header_and_json(tmp_path: Path, headers: dict, status: int
                                                    ) -> None:
    executor = AsyncMock(spec=DaemonChannel)
    app = build_app(ingress_state(tmp_path, executor), ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/replan", content=b"{}",
                               headers={"X-Remote-User-Id": "owner", **headers})
    assert response.status_code == status
    executor.submit.assert_not_called()


async def test_replan_reaches_submit_with_the_ingress_user(tmp_path: Path) -> None:
    executor = AsyncMock(spec=DaemonChannel)
    executor.submit.return_value = Reply(lines=action_lines(["[SKIP] normal programming held"]))
    state = ingress_state(tmp_path, executor)
    state.plan_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    app = build_app(state, ingress_peer="proxy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                                 base_url="http://local") as client:
        response = await client.post("/api/replan", json={}, headers={
            "X-Remote-User-Id": "owner_1", "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == 200
    assert response.json() == {
        "generated_at": state.plan_at.isoformat(),
        "lines": ["[SKIP] normal programming held"],
    }
    executor.submit.assert_awaited_once_with("replan", ingress_user="owner_1")


async def test_replan_refuses_a_non_ingress_peer(tmp_path: Path) -> None:
    app = build_app(ingress_state(tmp_path), ingress_peer="proxy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("spoof", 1)),
                                 base_url="http://local") as client:
        response = await client.post("/api/replan", json={}, headers={
            "X-Forwarded-For": "proxy", "X-Remote-User-Id": "owner",
            "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == 403


def test_replan_without_an_executor_is_unavailable(tmp_path: Path) -> None:
    app = build_app(ingress_state(tmp_path), ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/replan", json={}, headers={
            "X-Remote-User-Id": "owner", "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == 503
    assert response.json() == {"error": "no daemon executor"}


@pytest.mark.parametrize("error,status", [
    ("daemon stopped", 503),
    ("daemon request queue full", 503),
    ("configuration_repair", 503),
    ("daemon plan request failed", 500),
    ("device action refused", 500),
])
def test_replan_maps_channel_errors_to_fixed_text(tmp_path: Path, error: str, status: int) -> None:
    executor = AsyncMock(spec=DaemonChannel)
    executor.submit.return_value = Reply(error=error, text="SECRET-UPSTREAM", reasons=["SECRET"])
    app = build_app(ingress_state(tmp_path, executor), ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/replan", json={}, headers={
            "X-Remote-User-Id": "owner", "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == status
    if status == 500:
        # Any other error is fixed text; never the reply's own wording.
        assert response.json() == {"error": "replan failed"}
    else:
        assert response.json() == {"error": error}
    assert "SECRET-UPSTREAM" not in response.text


async def test_replan_reply_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ha_spark.api import server

    class Executor:
        async def submit(self, op, *, ingress_user=None):
            await asyncio.sleep(10)

    monkeypatch.setattr(server, "_MODE_REPLY_TIMEOUT_S", 0.01)
    app = build_app(ingress_state(tmp_path, Executor()), ingress_peer="proxy")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                                base_url="http://local") as client:
        response = await client.post("/api/replan", json={}, headers={
            "X-Remote-User-Id": "owner_1", "X-HA-Spark-Request": "replan",
        })
    assert response.status_code == 504
    assert response.json()["error"] == "pending"


@respx.mock
async def test_replan_through_the_route_writes_nothing_in_observe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        ha_url="http://ha.test", ha_token="SECRET", db_path=str(tmp_path / "t.db"),
        agent_surface="on", agent_exposure="read_act",
    )
    seed_settings(tmp_path, settings=settings)
    seed_authority(settings, "observe")
    respx.get(url__regex=r"http://ha.test/api/.*").mock(return_value=Response(200, json=[]))
    respx.post(url__regex=r"http://ha.test/api/.*").mock(return_value=Response(200, json=[]))
    with caplog.at_level("INFO"):
        async with running_daemon(settings, tmp_path, monkeypatch) as (state, calls, device, _):
            app = build_app(state, ingress_peer="proxy")
            before = len(calls)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                base_url="http://local",
            ) as client:
                response = await asyncio.wait_for(client.post("/api/replan", json={}, headers={
                    "X-Remote-User-Id": "owner", "X-HA-Spark-Request": "replan",
                }), 5)
            assert response.status_code == 200
            body = response.json()
            assert body["generated_at"] is not None
            assert any("[SIMULATE]" in line or "[SKIP]" in line for line in body["lines"])
            # One request, one replan run; Observe writes nothing.
            assert len(calls) == before + 1
        device.apply.assert_not_called()
    # The request is logged under the HA user id, and nothing else of it.
    assert "Replan requested requester=owner" in caplog.text
    assert "SECRET" not in caplog.text
