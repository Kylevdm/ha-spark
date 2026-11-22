"""The ingress statistics-repair routes: owner-only start, one run at a time."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy.ledger_repair import (
    RepairRun,
    _finish_repair,
    claim_repair,
    last_repair,
)
from tests.test_owner_mode import settings_at

NOW = datetime(2026, 1, 12, 12, tzinfo=UTC)
OWNER = {"X-Remote-User-Id": "owner", "X-HA-Spark-Request": "repair_stats"}


def ingress_state(tmp_path) -> AppState:
    return AppState(settings_at(tmp_path), tmp_path / "options.json")


def done_run() -> RepairRun:
    return RepairRun(status="done", started_at=NOW, finished_at=NOW,
                     checked_days=3, fixed_slots=5, still_gaps=2)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_repair_routes_absent_outside_ingress(tmp_path, method, token, peer):
    app = build_app(ingress_state(tmp_path), require_token=token, token="TOKEN",
                    ingress_peer=peer)
    with TestClient(app) as client:
        response = client.request(method, "/api/statistics/repair", json={}, headers={
            "Authorization": "Bearer TOKEN", **OWNER,
        })
    assert response.status_code == 404


@pytest.mark.parametrize("headers,status", [
    ({}, 403),
    ({"X-HA-Spark-Request": "mode"}, 403),
    ({"X-HA-Spark-Request": "repair_stats"}, 415),
    ({"X-HA-Spark-Request": "repair_stats", "Content-Type": "text/plain"}, 415),
])
def test_repair_post_requires_the_owner_headers(tmp_path, headers, status):
    state = ingress_state(tmp_path)
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/statistics/repair", content=b"{}",
                               headers={"X-Remote-User-Id": "owner", **headers})
    assert response.status_code == status
    assert state.statistics_repair_task is None
    # No claim was written: a refused request leaves no trace.
    assert not Path(state.settings.db_path).exists()


async def test_repair_post_claims_and_starts_the_background_job(tmp_path, monkeypatch):
    state = ingress_state(tmp_path)
    started = []

    async def fake_run(settings: Settings, repair_id: int, now: datetime) -> RepairRun:
        started.append((repair_id, settings is state.settings_service.settings))
        return done_run()

    monkeypatch.setattr(server, "run_ledger_repair", fake_run)
    app = build_app(state, ingress_peer="testclient")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("testclient", 1)),
        base_url="http://local",
    ) as client:
        response = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert response.status_code == 202
        assert response.json() == {"status": "running"}
        assert state.statistics_repair_task is not None
        await state.statistics_repair_task
        # The claim's running row is what a second request and the page read.
        status = await client.get("/api/statistics/repair")
    assert started and started[0][1]
    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    run = status.json()["last_run"]
    assert (run["status"], run["finished_at"], run["checked_days"]) == ("running", None, None)


async def test_a_second_request_is_refused_while_the_first_runs(tmp_path, monkeypatch):
    state = ingress_state(tmp_path)
    release = asyncio.Event()

    async def slow_run(settings: Settings, repair_id: int, now: datetime) -> RepairRun:
        await release.wait()
        # Like the real job, finish the claimed row so the claim is released.
        await _finish_repair(settings.db_path, repair_id, "done", now, 3, 5, 2)
        return done_run()

    monkeypatch.setattr(server, "run_ledger_repair", slow_run)
    app = build_app(state, ingress_peer="testclient")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("testclient", 1)),
        base_url="http://local",
    ) as client:
        first = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert first.status_code == 202
        second = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert second.status_code == 409
        assert second.json() == {"error": "repair already running"}
        release.set()
        await state.statistics_repair_task
        # The finished run releases the claim: a new request starts again.
        third = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert third.status_code == 202
        await state.statistics_repair_task
    assert (await last_repair(state.settings.db_path)).status == "done"


async def test_repair_get_returns_the_last_run(tmp_path):
    settings = settings_at(tmp_path)
    state = AppState(settings, tmp_path / "options.json")
    repair_id = await claim_repair(settings.db_path, NOW)
    assert repair_id is not None
    await _finish_repair(settings.db_path, repair_id, "done", NOW, 3, 5, 2)
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get("/api/statistics/repair")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"last_run": {
        "status": "done", "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(),
        "checked_days": 3, "fixed_slots": 5, "still_gaps": 2,
    }}


async def test_a_failed_status_read_reports_unavailable(tmp_path, monkeypatch):
    # An unreadable run table is a fixed-text failure, not a silent "never run",
    # so Settings shows that the status is unavailable instead of inventing it.
    state = ingress_state(tmp_path)

    async def broken(db_path, **kwargs):
        raise RuntimeError("private storage detail")

    monkeypatch.setattr(server, "last_repair", broken)
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get("/api/statistics/repair")
    assert response.status_code == 503
    assert response.json() == {"error": "repair status unavailable"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "private storage detail" not in response.text


async def test_started_and_refused_posts_are_never_cached(tmp_path, monkeypatch):
    # A successful start and a concurrent refusal both carry no-store.
    state = ingress_state(tmp_path)
    release = asyncio.Event()

    async def slow_run(settings: Settings, repair_id: int, now: datetime) -> RepairRun:
        await release.wait()
        await _finish_repair(settings.db_path, repair_id, "done", now, 3, 5, 2)
        return done_run()

    monkeypatch.setattr(server, "run_ledger_repair", slow_run)
    app = build_app(state, ingress_peer="testclient")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("testclient", 1)),
        base_url="http://local",
    ) as client:
        first = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert first.status_code == 202
        assert first.headers["Cache-Control"] == "no-store"
        second = await client.post("/api/statistics/repair", json={}, headers=OWNER)
        assert second.status_code == 409
        assert second.headers["Cache-Control"] == "no-store"
        release.set()
        await state.statistics_repair_task


@pytest.mark.parametrize("headers,status", [
    ({}, 403),
    ({"X-HA-Spark-Request": "repair_stats"}, 415),
])
def test_owner_gate_refusals_are_never_cached(tmp_path, headers, status):
    state = ingress_state(tmp_path)
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/statistics/repair", content=b"{}",
                               headers={"X-Remote-User-Id": "owner", **headers})
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"


def test_a_claim_failure_refusal_is_never_cached(tmp_path, monkeypatch):
    state = ingress_state(tmp_path)

    async def broken(*args, **kwargs):
        raise RuntimeError("private claim detail")

    monkeypatch.setattr(server, "claim_repair", broken)
    app = build_app(state, ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.post("/api/statistics/repair", json={}, headers=OWNER)
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"


async def test_repair_get_without_history_shows_none(tmp_path):
    app = build_app(ingress_state(tmp_path), ingress_peer="testclient")
    with TestClient(app) as client:
        response = client.get("/api/statistics/repair")
    assert response.status_code == 200
    assert response.json() == {"last_run": None}
    assert (await last_repair(settings_at(tmp_path).db_path)) is None
