"""Native SoC health uses allowlisted daemon diagnostics, never REST substitutes."""
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.api.server import AppState
from ha_spark.api.snapshot_v4 import BankSoc
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply
from tests.native_helpers import NOW, native_link


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    return Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db"))


@pytest.mark.parametrize("condition,status,age", [
    ("trusted", "ok", 0), ("missing", "unavailable", None),
    ("stale", "stale", 86400), ("future", "report_time_future", -1),
    ("untrusted", "implausible", 0), ("heartbeat", "unavailable", None),
])
async def test_native_soc_health_socket(settings, monkeypatch, condition, status, age):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("ha_spark.api.server.datetime", Clock)
    link = native_link(settings)
    bank = link.v4.banks[0]
    soc = bank.soc
    if condition == "missing":
        soc = None
    elif condition == "stale":
        soc = soc.model_copy(update={"read_at": NOW - timedelta(days=1)})
    elif condition == "future":
        soc = soc.model_copy(update={"read_at": NOW + timedelta(seconds=1)})
    elif condition == "untrusted":
        soc = soc.model_copy(update={"trust": "untrusted"})
    elif condition == "heartbeat":
        link = replace(link, received_at=NOW - timedelta(seconds=91))
    link = replace(link, v4=replace(link.v4, banks=(BankSoc(id=bank.id, soc=soc),)))
    async with DaemonChannel(settings) as channel:
        state = AppState(settings, Path(settings.db_path).parent / "options.json", executor=channel)
        state.integration_link = link
        channel.link_view = state.integration_link_view
        view = state.integration_link_view()
        assert view["native_soc_status"] == status
        assert view["native_soc_age_s"] == age
        assert view["native_soc_pct"] == (None if age is None else 42.0)
        row = await health.check_native_soc(settings)
    assert row.name == "Native SoC"
    if status == "ok":
        assert row.status is health.Status.OK
        assert row.detail == "snapshot SoC 42%; trusted; read 0s ago"
    else:
        assert row.status is health.Status.FAIL
        assert row.detail == f"snapshot SoC {status}; SoC-derived writes are blocked"


@pytest.mark.parametrize("view", [
    {}, {"native_soc_status": "CANARY"}, {"native_soc_status": []},
    *({"native_soc_status": "ok", "native_soc_pct": pct, "native_soc_age_s": age}
      for pct, age in [(True, 0), (42, True), (42, -1), (42, 0.5), (42, "CANARY"),
                       ("CANARY", 0), (float("nan"), 0), (float("inf"), 0),
                       (-1, 0), (101, 0), (None, 0), (10**400, 0)]),
])
async def test_native_soc_health_untrusted_reply(settings, monkeypatch, view):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(
        return_value=Reply(integration_link=view),
    ))
    row = await health.check_native_soc(settings)
    assert row == health.CheckResult("Native SoC", health.Status.WARN, "unknown")


@pytest.mark.parametrize("failure,detail", [
    (None, "unknown (no daemon running)"),
    (OSError("CANARY"), "unknown (daemon connection lost)"),
])
async def test_native_soc_health_daemon_unreachable(settings, monkeypatch, failure, detail):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=None, side_effect=failure))
    row = await health.check_native_soc(settings)
    assert row == health.CheckResult("Native SoC", health.Status.WARN, detail)


@pytest.mark.parametrize("retired", [False, True])
def test_native_entity_requirements(settings, retired):
    if retired:
        store = AuthorityStore(settings)
        store.initialise()
        assert store.handover("pending_integration") == "retired"
    row = health.check_entity_config(settings)
    for field in ("soc_entity", "battery_voltage_entity", "charge_current_entity",
                  "inverter_power_switch_entity"):
        assert (field in row.detail) is not retired
    assert "battery_power_entity" in row.detail


@pytest.mark.parametrize("retired", [False, True])
async def test_native_soc_row_only_when_retired(settings, monkeypatch, retired):
    if retired:
        store = AuthorityStore(settings)
        store.initialise()
        assert store.handover("pending_integration") == "retired"
    for name in dir(health):
        if name.startswith("check_"):
            stub = health.CheckResult(name, health.Status.OK, "")
            monkeypatch.setattr(health, name, AsyncMock(return_value=stub))
    monkeypatch.setattr(health, "check_entity_config", lambda _: stub)
    monkeypatch.setattr(health, "check_hold_release", lambda _: [])
    monkeypatch.setattr(health, "check_discharge_ceiling", lambda *_: [])
    monkeypatch.setattr(health, "check_evidence_freshness", AsyncMock(return_value=[]))
    rows = await health.run_health(settings)
    assert any(row.name == "check_native_soc" for row in rows) is retired
