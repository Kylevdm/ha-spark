"""Authority-owner diagnostics reveal only allowlisted owner and freshness facts."""
from unittest.mock import AsyncMock

import pytest

from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply
from ha_spark.health import Status, check_authority_owner, check_operating_mode


@pytest.mark.parametrize("owner,state,status,detail", [
    ("addon", "connected", Status.OK, "handshake settled; snapshot fresh"),
    ("integration", "connected", Status.OK, "handshake settled; snapshot fresh"),
    ("pending_integration", "connected", Status.WARN, "handshake pending; snapshot fresh"),
    ("pending_addon", "connected", Status.WARN, "handshake pending; snapshot fresh"),
    ("integration", "unreachable", Status.WARN, "snapshot unavailable"),
    ("integration", "version_skew", Status.WARN, "snapshot unavailable"),
    ("SECRET-CANARY", "connected", Status.WARN, "unknown; snapshot unavailable"),
])
async def test_owner_health(owner, state, status, detail, monkeypatch):
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(return_value=Reply(
        integration_link={"authority_owner": owner, "state": state},
    )))
    result = await check_authority_owner(Settings())
    assert result.status is status
    assert detail in result.detail
    assert "SECRET-CANARY" not in result.detail


async def test_native_operating_mode_does_not_treat_retirement_as_damage(tmp_path, monkeypatch):
    from ha_spark.authority import AuthorityStore

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(return_value=Reply(
        authority={"authority_owner": "integration", "mode": "optimise", "save_failed": False},
    )))
    result = await check_operating_mode(settings)
    assert result.status is Status.OK
    assert "optimise" in result.detail


@pytest.mark.parametrize("reply", [None, Reply(integration_link={"authority_owner": "addon",
                                                                  "state": "unreachable"})])
async def test_owner_health_retired_marker_wins_over_missing_snapshot(tmp_path, monkeypatch, reply):
    from ha_spark.authority import AuthorityStore

    settings = Settings(ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"))
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    monkeypatch.setattr("ha_spark.health.submit_socket", AsyncMock(return_value=reply))
    result = await check_authority_owner(settings)
    assert result.status is Status.WARN
    assert result.detail == "integration; handshake settled; snapshot unavailable"
