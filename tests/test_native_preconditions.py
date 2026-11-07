"""Retired-site entry diagnostics use snapshot SoC without legacy entity reads."""
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import respx

from ha_spark.api.native_apply import snapshot_soc
from ha_spark.api.server import AppState
from ha_spark.api.snapshot_v4 import BankSoc
from ha_spark.authority import AuthorityStore
from ha_spark.config import DeviceConfig, Settings
from ha_spark.daemon_channel import DaemonChannel
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.mode_preconditions import check_preconditions, entry_preconditions
from tests.api_helpers import owner_client
from tests.native_helpers import NOW, native_link


@pytest.fixture
def settings(tmp_path):
    settings = Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "db"),
        devices=[DeviceConfig(id="solis", type="inverter", driver="solis")],
        ev_status_entity="sensor.ev", battery_power_entity="sensor.battery",
        solis_fallback_current_a=10,
    )
    store = AuthorityStore(settings)
    store.initialise()
    assert store.handover("pending_integration") == "retired"
    return settings


@pytest.mark.parametrize("condition,status", [
    ("trusted", None), ("missing", "unavailable"), ("stale", "stale"),
    ("future", "report_time_future"), ("untrusted", "implausible"),
    ("heartbeat", "unavailable"), ("none", "unavailable"),
])
@respx.mock
async def test_native_entry_soc_and_no_legacy_reads(settings, monkeypatch, condition, status):
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
    measurement = None if condition == "none" else snapshot_soc(link, NOW, settings)
    observe = AsyncMock(side_effect=AssertionError("REST SoC must not be read"))
    monkeypatch.setattr("ha_spark.mode_preconditions.observe_soc", observe)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        report = await check_preconditions(
            settings, rest, measurement, soc_programming_held=False,
        )
    reasons = () if status is None else (
        f"native snapshot SoC must be trusted and fresh ({status})",
    )
    assert report.protect == ()
    assert report.optimise == reasons
    # Diagnostics never take snapshot SoC (step-7 L3): always unavailable here.
    entry = await entry_preconditions(settings)
    assert entry["protect"] == {"passed": True, "reasons": []}
    assert entry["optimise"] == {"passed": False, "reasons": [
        "native snapshot SoC must be trusted and fresh (unavailable)"]}
    observe.assert_not_awaited()
    assert not respx.calls


@pytest.mark.parametrize("present", [True, False])
@respx.mock
async def test_native_mode_api_reports_checked_snapshot_soc(
    settings, monkeypatch, tmp_path, present,
):
    """A live integration snapshot keeps the integration's own check; without one, SoC fails."""
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("ha_spark.api.server.datetime", Clock)
    monkeypatch.setattr("ha_spark.mode_preconditions.observe_soc", AsyncMock(
        side_effect=AssertionError("REST SoC must not be read"),
    ))
    async with DaemonChannel(settings) as channel:
        state = AppState(settings, tmp_path / "options.json", executor=channel)
        state.integration_link = native_link(settings)
        if not present:
            state.integration_link = replace(state.integration_link, v4=None)
        with owner_client(state) as client:
            response = client.get("/api/mode")
    assert response.status_code == 200
    report = response.json()["preconditions"]
    if present:
        unchecked = {"passed": False, "reasons": ["checked by the integration when requested"]}
        assert report == {target: unchecked for target in ("observe", "protect", "optimise")}
    else:
        assert report["protect"] == {"passed": True, "reasons": []}
        assert report["optimise"] == {
            "passed": False,
            "reasons": ["native snapshot SoC must be trusted and fresh (unavailable)"],
        }
    assert not respx.calls


@respx.mock
async def test_native_entry_preserves_programming_hold(settings):
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        report = await check_preconditions(
            settings, rest, snapshot_soc(native_link(settings), NOW, settings),
            soc_programming_held=True,
        )
    assert report.protect == ()
    assert report.optimise == ("SoC fallback or recovery is in progress; wait for SoC recovery",)
    assert not respx.calls
