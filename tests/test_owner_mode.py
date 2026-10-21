"""Owner-only operating-mode requests and durable handovers."""
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply


def settings_at(path: Path) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="SECRET", db_path=str(path / "t.db"))


def test_confirm_observe_recovers_unknown_without_hardware(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    store = AuthorityStore(settings)
    store.start_process()
    store.store_path.unlink()
    store.confirm_observe("cli")
    assert store.known
    assert store.state.committed_mode == "observe"
    assert store.state.epoch == 1
    assert store.state.holds == ()


@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_mode_route_absent_outside_ingress(tmp_path: Path, token: bool, peer: str | None) -> None:
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    require_token=token, token="TOKEN", ingress_peer=peer)
    with TestClient(app) as client:
        response = client.post("/api/mode", json={"target": "protect", "expected_epoch": 1},
                               headers={"Authorization": "Bearer TOKEN",
                                        "X-Remote-User-Id": "owner"})
    assert response.status_code == 404


@pytest.mark.parametrize("user", [None, "", "name with spaces", "a\nforged", "a" * 65])
def test_mode_ingress_requires_valid_user(tmp_path: Path, user: str | None) -> None:
    app = build_app(AppState(settings_at(tmp_path), tmp_path / "options.json"),
                    ingress_peer="testclient")
    headers = {} if user is None else {"X-Remote-User-Id": user}
    with TestClient(app) as client:
        assert client.post("/api/mode", json={"target": "protect", "expected_epoch": 1},
                           headers=headers).status_code == 403


async def test_mode_ingress_queues_owner_and_epoch(tmp_path: Path) -> None:
    class Executor:
        async def submit_mode(self, target, expected_epoch, *, ingress_user=None):
            assert (target, expected_epoch, ingress_user) == ("protect", 7, "owner_1")
            return Reply(authority={"mode": "protect", "epoch": 8})

    state = AppState(settings_at(tmp_path), tmp_path / "options.json", executor=Executor())
    app = build_app(state, ingress_peer="proxy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                                base_url="http://local") as client:
        response = await client.post("/api/mode", json={"target": "protect", "expected_epoch": 7},
                                     headers={"X-Remote-User-Id": "owner_1",
                                              "X-HA-Spark-Request": "mode"})
    assert response.status_code == 200
    assert response.json()["epoch"] == 8


def test_mode_cli_parser() -> None:
    assert cli.build_parser().parse_args(["mode", "set", "protect"]).target == "protect"
    args = cli.build_parser().parse_args(["mode", "confirm-observe"])
    assert args.mode_command == "confirm-observe"


def test_ingress_panel_stays_admin_only() -> None:
    import yaml

    config = yaml.safe_load(Path("ha_spark_addon/config.yaml").read_text())
    assert config.get("panel_admin", True) is True


async def test_mode_ingress_reply_is_bounded(tmp_path: Path, monkeypatch) -> None:
    from ha_spark.api import server

    class Executor:
        async def submit_mode(self, target, expected_epoch, *, ingress_user=None):
            await asyncio.sleep(10)

    monkeypatch.setattr(server, "_MODE_REPLY_TIMEOUT_S", 0.01)
    state = AppState(settings_at(tmp_path), tmp_path / "options.json", executor=Executor())
    app = build_app(state, ingress_peer="proxy")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("proxy", 1)),
                                base_url="http://local") as client:
        response = await client.post("/api/mode", json={"target": "protect", "expected_epoch": 7},
                                     headers={"X-Remote-User-Id": "owner_1",
                                              "X-HA-Spark-Request": "mode"})
    assert response.status_code == 504
