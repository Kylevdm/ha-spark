"""Read-only integration link diagnostics across HTTP, socket and health (#480)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark import cli, health
from ha_spark.api.contract import IntegrationLink, SiteSnapshot, link_view
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply, submit_socket
from ha_spark.integration_discovery import IntegrationDiscovery
from tests.api_helpers import owner_client

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
KEYS = {"state", "site_id", "integration_release_version", "api_version", "sent_at",
        "snapshot_age_s"}
TOKEN = "TOKEN-CANARY-480"
SESSION = "SESSION-CANARY-480"


def snapshot() -> SiteSnapshot:
    return SiteSnapshot.model_validate({
        "api_version": 1, "release_version": "0.25.0",
        "site_id": "12345678-1234-5678-1234-567812345678", "sent_at": NOW.isoformat(),
        "auth_token": TOKEN, "session_id": SESSION,
    })


@pytest.mark.parametrize(("age", "skew", "expected"), [
    (None, False, "never_connected"), (None, True, "version_skew"),
    (0, False, "connected"), (90, False, "connected"), (91, False, "unreachable"),
    (91, True, "version_skew"), (-5, False, "connected"),
])
def test_link_view(age: int | None, skew: bool, expected: str) -> None:
    link = IntegrationLink(snapshot() if age is not None else None,
                           NOW - timedelta(seconds=age) if age is not None else None, skew)
    view = link_view(link, NOW)
    assert view == {
        "state": expected,
        "site_id": snapshot().site_id if age is not None else None,
        "integration_release_version": "0.25.0" if age is not None else None,
        "api_version": 1 if age is not None else None,
        "sent_at": NOW.isoformat() if age is not None else None,
        "snapshot_age_s": max(0, age) if age is not None else None,
    }
    assert set(view) == KEYS
    assert TOKEN not in json.dumps(view) and SESSION not in json.dumps(view)


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize(("age", "skew", "expected"), [
    (None, False, "never_connected"), (0, False, "connected"),
    (91, False, "unreachable"), (0, True, "version_skew"),
])
async def test_link_http_socket_and_health(
    tmp_path: Path, repair: bool, age: int | None, skew: bool, expected: str,
) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token=TOKEN,
                        db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    if repair:
        state.settings_service.repair = ["invalid configuration"]
    state.integration_link = IntegrationLink(
        snapshot() if age is not None else None,
        datetime.now(UTC) - timedelta(seconds=age) if age is not None else None, skew,
    )
    with owner_client(state) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    view = response.json()["integration_link"]
    assert view["state"] == expected and set(view) == KEYS
    if repair:
        assert response.json()["configuration"]["status"] == "configuration_repair"
    async with DaemonChannel(settings) as channel:
        channel.settings_service = state.settings_service
        channel.link_view = state.integration_link_view
        reply = await submit_socket(settings, "integration_link")
        assert reply is not None and reply.integration_link == view
        result = await health.check_integration_link(settings)
        assert result.name == "Integration link"
        status = health.Status.OK if expected == "connected" else health.Status.WARN
        assert result.status is status
        assert expected in result.detail
        assert health.exit_code([result]) == (0 if expected == "connected" else 2)
        if age is not None:
            assert "0.25.0" in result.detail
        report = health.format_report([result])
        rendered = response.text + reply.model_dump_json() + report
        assert TOKEN not in rendered and SESSION not in rendered
        if repair:
            results = await health.run_repair_health(settings, ["invalid configuration"])
            assert result in results


async def test_link_no_daemon(tmp_path: Path) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token=TOKEN,
                        db_path=str(tmp_path / "ha_spark.db"))
    result = await health.check_integration_link(settings)
    assert result.status is health.Status.OK
    assert result.detail == "unknown (no daemon running)"
    assert health.exit_code([result]) == 0


@pytest.mark.parametrize("reply", [Reply(), Reply(error="unavailable")])
async def test_link_unexpected_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: Reply,
) -> None:
    async def submit(*args: object) -> Reply:
        return reply

    monkeypatch.setattr(health, "submit_socket", submit)
    result = await health.check_integration_link(
        Settings(ha_url="http://ha.test", ha_token=TOKEN, db_path=str(tmp_path / "ha_spark.db")),
    )
    assert result.status is health.Status.WARN and result.detail == "unknown"


async def test_socket_link_without_provider(tmp_path: Path) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token=TOKEN,
                        db_path=str(tmp_path / "ha_spark.db"))
    async with DaemonChannel(settings):
        reply = await submit_socket(settings, "integration_link")
    assert reply is not None
    assert reply.integration_link == link_view(IntegrationLink(None, None, False), NOW)


def test_heartbeat_health_and_skew_recovery(tmp_path: Path) -> None:
    settings = Settings(supervisor_token="unused", db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    discovery = IntegrationDiscovery(supervisor_token="unused", host="addon",
                                     auth_token=TOKEN, session_id=SESSION)
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        initial = client.get("/api/health").json()
        assert initial["integration_link"]["state"] == "never_connected"
        body = snapshot().model_dump(mode="json")
        for version, status, expected in [(1, 200, "connected"), (2, 409, "version_skew"),
                                           (1, 200, "connected")]:
            body["api_version"] = version
            assert client.post("/integration/heartbeat", json=body, headers={
                "Authorization": f"Bearer {TOKEN}",
            }).status_code == status
            response = client.get("/api/health")
            assert response.json()["integration_link"]["state"] == expected
            assert response.json()["status"] == initial["status"]
            assert TOKEN not in response.text and SESSION not in response.text


@pytest.mark.parametrize(("age", "skew", "expected"), [
    (None, False, "never_connected"), (0, False, "connected"),
    (91, False, "unreachable"), (0, True, "version_skew"),
])
async def test_cli_health_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    age: int | None, skew: bool, expected: str,
) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token=TOKEN,
                        db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    state.integration_link = IntegrationLink(
        snapshot() if age is not None else None,
        datetime.now(UTC) - timedelta(seconds=age) if age is not None else None, skew,
    )

    async def ok(settings: Settings) -> health.CheckResult:
        return health.CheckResult("Dependency", health.Status.OK, "ok")

    async def evidence(settings: Settings) -> list[health.CheckResult]:
        return []

    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    for name in (
        "check_operating_mode", "check_ha_rest", "check_ha_websocket", "check_ollama",
        "check_sqlite", "check_load_history", "check_supply_guard", "check_tariff_provider",
        "check_export_event_provider", "check_ha_timezone",
    ):
        monkeypatch.setattr(health, name, ok)
    monkeypatch.setattr(health, "check_evidence_freshness", evidence)
    async with DaemonChannel(settings) as channel:
        channel.link_view = state.integration_link_view
        code = await asyncio.to_thread(cli.main, ["health"])
    output = capsys.readouterr().out
    assert "Integration link" in output and expected in output
    assert TOKEN not in output and SESSION not in output
    assert code in (0, 2)  # Existing configuration warnings may also degrade health, never fail it.
    if expected != "connected":
        assert code == 2


@pytest.mark.parametrize("hostile", [
    {"integration_release_version": "\x1b[2J" + SESSION},
    {"snapshot_age_s": {"session_id": SESSION}},
    {"snapshot_age_s": True},
])
async def test_cli_prints_only_validated_link_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hostile: dict[str, object],
) -> None:
    async def submit(*args: object) -> Reply:
        return Reply(integration_link={"state": "connected", **hostile})

    monkeypatch.setattr(health, "submit_socket", submit)
    result = await health.check_integration_link(
        Settings(ha_url="http://ha.test", ha_token=TOKEN, db_path=str(tmp_path / "ha_spark.db")),
    )
    assert result.detail == "connected"
    assert SESSION not in result.detail and "\x1b" not in result.detail


async def test_lost_daemon_connection_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def submit(*args: object) -> Reply:
        raise ConnectionResetError

    monkeypatch.setattr(health, "submit_socket", submit)
    result = await health.check_integration_link(
        Settings(ha_url="http://ha.test", ha_token=TOKEN, db_path=str(tmp_path / "ha_spark.db")),
    )
    assert result.status is health.Status.WARN
    assert result.detail == "unknown (daemon connection lost)"


async def test_channel_exit_unbinds_the_link_view(tmp_path: Path) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token=TOKEN,
                        db_path=str(tmp_path / "ha_spark.db"))
    channel = DaemonChannel(settings)
    async with channel:
        channel.link_view = lambda: {"state": "connected"}
    assert channel.link_view is None
