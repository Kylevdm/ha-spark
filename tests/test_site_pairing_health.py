"""Pairing health exposes only trusted pairing and canonical refused site ids."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.api.server import AppState
from ha_spark.api.site_pairing import PAIRING_FILE, save_pairing
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply
from ha_spark.health import Status, check_site_pairing

OLD = "00000000-0000-0000-0000-000000000001"
NEW = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    return Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db"))


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("refused", [None, NEW])
async def test_pairing_health_socket(settings, paired, refused):
    if paired:
        assert save_pairing(settings, OLD)
    async with DaemonChannel(settings) as channel:
        state = AppState(settings, Path(settings.db_path).parent / "options.json", executor=channel)
        channel.link_view = state.integration_link_view
        state.refused_site = refused
        row = await check_site_pairing(settings)
    expected = f"paired {OLD}" if paired else "not paired"
    if refused:
        expected += f"; last refused {NEW} (site_not_paired; see ha-spark pair --replace)"
    assert row.name == "Site pairing"
    assert row.detail == expected
    assert row.status is (Status.WARN if refused else Status.OK)


@pytest.mark.parametrize("refused", [OLD, OLD.replace("-", ""), "CANARY" * 100, 123, {}, None])
async def test_pairing_health_ignores_untrusted_reply(settings, monkeypatch, refused):
    assert save_pairing(settings, OLD)
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(return_value=Reply(
        integration_link={"refused_site_id": refused, "snapshot": "CANARY"}
    )))
    row = await check_site_pairing(settings)
    assert row.status is Status.OK
    assert row.detail == f"paired {OLD}"


@pytest.mark.parametrize("failure", [None, OSError("CANARY")])
async def test_pairing_health_daemon_unreachable(settings, monkeypatch, failure):
    assert save_pairing(settings, OLD)
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(
        return_value=None, side_effect=failure,
    ))
    row = await check_site_pairing(settings)
    assert row.status is Status.OK
    assert row.detail == f"paired {OLD}"


async def test_pairing_health_corrupt_record(settings):
    (Path(settings.db_path).parent / PAIRING_FILE).write_bytes(b"CANARY")
    row = await check_site_pairing(settings)
    assert row.status is Status.WARN
    assert row.detail == "pairing record untrusted"


@pytest.mark.parametrize("repair", [False, True])
async def test_pairing_row_in_health_reports(settings, monkeypatch, repair):
    (Path(settings.db_path).parent / PAIRING_FILE).write_bytes(b"CANARY")
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=None))
    if repair:
        rows = await health.run_repair_health(settings, ["invalid configuration"])
    else:
        for name in dir(health):
            if name.startswith("check_") and name != "check_site_pairing":
                stub = health.CheckResult(name, Status.OK, "")
                monkeypatch.setattr(health, name, AsyncMock(return_value=stub))
        monkeypatch.setattr(health, "check_entity_config", lambda _: stub)
        monkeypatch.setattr(health, "check_hold_release", lambda _: [])
        monkeypatch.setattr(health, "check_discharge_ceiling", lambda *_: [])
        monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
        rows = await health.run_health(settings)
    row = next(r for r in rows if r.name == "Site pairing")
    assert row.status is Status.WARN and row.detail == "pairing record untrusted"
