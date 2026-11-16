"""Health on native sites reports the integration's hold gates, not add-on options."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_spark import health
from ha_spark.config import Settings
from ha_spark.health import Status


def view(evidence):
    return SimpleNamespace(error=None, integration_link={
        "authority_owner": "integration", "state": "connected", "evidence": evidence})


async def test_integration_evidence_uses_snapshot_and_keeps_supplier(monkeypatch):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=view({
        "ev_status": {"validity": "fresh", "age_s": 12.0, "max_age_s": 123.0,
                      "via_sibling": True},
        "live_dispatch": {"validity": "stale", "age_s": 124.0, "max_age_s": 123.0,
                          "via_sibling": False}})))
    supplier = AsyncMock()
    monkeypatch.setattr(health, "fetch_planned_dispatches", supplier)
    rows = await health.check_evidence_freshness(Settings(
        supervisor_token="unused", tariff_provider="octopus_intelligent",
        octopus_dispatch_max_age_seconds=60))
    assert [r.name for r in rows] == ["Evidence ev_status", "Evidence live_dispatch",
                                     "Evidence octopus_dispatch"]
    assert rows[0].status == Status.OK
    assert rows[0].detail == "integration; fresh; age=12.0s; max-age=123.0s; sibling=yes"
    assert rows[1].status == Status.WARN
    assert rows[2].status == Status.OK and "max-age=60.0s; sibling=no" in rows[2].detail
    supplier.assert_awaited_once()


@pytest.mark.parametrize("change", [
    {"validity": "SECRET"}, {"age_s": True}, {"age_s": float("nan")},
    {"age_s": float("inf")}, {"age_s": 10 ** 1000}, {"age_s": -1}, {"age_s": None},
    {"max_age_s": True}, {"max_age_s": float("inf")}, {"max_age_s": 0},
    {"max_age_s": "SECRET"}, {"via_sibling": "SECRET"},
    {"age_s": 301.0}, {"validity": "stale", "via_sibling": True},
])
async def test_invalid_projection_never_prints_raw_values(monkeypatch, change):
    item = {"validity": "fresh", "age_s": 0.0, "max_age_s": 300.0,
            "via_sibling": False, **change}
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=view({"ev_status": item})))
    rows = await health.check_evidence_freshness(Settings(supervisor_token="unused"))
    assert len(rows) == 1
    assert rows[0].name == "Evidence" and rows[0].status == Status.WARN
    assert rows[0].detail == "integration evidence unavailable"


@pytest.mark.parametrize("raw", [None, {}, "SECRET", {"SECRET": {}}])
async def test_missing_or_unknown_sources_warn(monkeypatch, raw):
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=view(raw)))
    rows = await health.check_evidence_freshness(Settings(supervisor_token="unused"))
    assert len(rows) == 1 and rows[0].detail == "integration evidence unavailable"


@pytest.mark.parametrize("validity", ["unknown", "future"])
def test_missing_age_is_valid_for_unknown_or_future(validity):
    rows = health._integration_evidence_rows({"ev_status": {
        "validity": validity, "age_s": None, "max_age_s": 300, "via_sibling": False}})
    assert rows[0].status == Status.WARN
    assert f"integration; {validity}; age=Nones" in rows[0].detail


async def test_unreachable_link_hides_old_integration_evidence(monkeypatch):
    stale = view({"ev_status": {"validity": "fresh", "age_s": 1.0, "max_age_s": 300.0,
                                "via_sibling": False}})
    stale.integration_link["state"] = "unreachable"
    monkeypatch.setattr(health, "submit_socket", AsyncMock(return_value=stale))
    rows = await health.check_evidence_freshness(Settings(supervisor_token="unused"))
    assert [(r.name, r.status, r.detail) for r in rows] == [
        ("Evidence", Status.WARN, "integration evidence unavailable")]


async def test_sibling_never_claimed_for_ungrouped_sources(monkeypatch):
    item = {"validity": "fresh", "age_s": 1.0, "max_age_s": 300.0, "via_sibling": True}
    monkeypatch.setattr(health, "submit_socket", AsyncMock(
        return_value=view({"dispatch_grid_power": item})))
    rows = await health.check_evidence_freshness(Settings(supervisor_token="unused"))
    assert rows[0].detail == "integration evidence unavailable"
