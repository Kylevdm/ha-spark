"""Health re-validates the opt-in IPC view without trusting rejected display values."""

from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply

CANARY = "stored-admin-user-id-canary"
BLOCK = {
    "export_during_events": True,
    "export_enabled_at": "2026-10-24T11:00:00+00:00",
    "export_ratings_w": {"inv2": 6000.0, "inv1": 5000.0},
}


@pytest.mark.parametrize("change,detail", [
    ({}, "on since 2026-10-24T11:00:00+00:00; ratings inv1 5000 W, inv2 6000 W"),
    ({"export_during_events": False}, "off; ratings inv1 5000 W, inv2 6000 W"),
    ({"export_during_events": False, "export_ratings_w": {}}, "off; no ratings"),
])
async def test_on_off_and_sorted_ratings(monkeypatch, change, detail):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=Reply(
        integration_link={"state": "connected", "flexibility_events": BLOCK | change})))
    row = await health.check_flexibility_events(Settings())
    assert row == health.CheckResult("Export during events", health.Status.OK, detail)
    assert CANARY not in str(row)


@pytest.mark.parametrize("block", [
    None, [], CANARY, {},
    *(BLOCK | change for change in [
        {"export_enabled_by": CANARY}, {"export_during_events": 1},
        {"export_enabled_at": CANARY}, {"export_enabled_at": None},
        {"export_ratings_w": {}}, {"export_ratings_w": {CANARY + "\n": 5000}},
        {"export_ratings_w": {"inv1": float("nan")}},
        {"export_ratings_w": {"inv1": float("inf")}},
        {"export_ratings_w": {"inv1": True}},
        {"export_ratings_w": {"inv1": 100001}},
    ]),
])
async def test_invalid_projection_unavailable(monkeypatch, block):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=Reply(
        integration_link={"state": "connected", "flexibility_events": block})))
    row = await health.check_flexibility_events(Settings())
    assert row == health.CheckResult(
        "Export during events", health.Status.WARN, "integration state unavailable")
    assert CANARY not in str(row)


@pytest.mark.parametrize("state", [None, "unreachable", "never_connected", "version_skew"])
async def test_not_connected(monkeypatch, state):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=Reply(
        integration_link={"state": state, "flexibility_events": BLOCK})))
    assert (await health.check_flexibility_events(Settings())).status is health.Status.WARN


@pytest.mark.parametrize("failure", [None, OSError(CANARY), RuntimeError(CANARY)])
async def test_socket_failures_never_escape(monkeypatch, failure):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=None, side_effect=failure))
    row = await health.check_flexibility_events(Settings())
    assert row.detail == "integration state unavailable"
    assert row.status is health.Status.WARN
