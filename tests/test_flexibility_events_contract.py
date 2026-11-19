"""Owner opt-in is optional display data, never a snapshot acceptance gate."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ha_spark.api.contract import IntegrationLink, link_view
from ha_spark.api.snapshot_v4 import parse_snapshot_v4
from ha_spark.energy.topology import Topology

CANARY = "stored-admin-user-id-canary"
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def facts():
    raw = json.loads((Path(__file__).parent / "fixtures" / "contract" /
                      "site_snapshot_v4_max.json").read_text())
    return raw, Topology.model_validate(raw["topology"]["topology"])


def test_valid_projection_and_owner_scope():
    raw, topology = facts()
    parsed = parse_snapshot_v4(raw, topology)
    assert parsed.flexibility_events is not None
    assert parsed.flexibility_events_reason is None
    link = IntegrationLink(None, None, False, v4=parsed)
    view = link_view(link, NOW)
    assert view["flexibility_events"] == {
        **raw["flexibility_events"], "export_enabled_at": "2026-10-24T11:00:00Z",
    }
    assert CANARY not in json.dumps(view)
    assert "export_enabled_by" not in json.dumps(view)
    for owner in ("addon", "pending_integration", "pending_addon"):
        other = parse_snapshot_v4(raw | {"authority_owner": owner}, topology)
        assert other.flexibility_events is None
        assert other.flexibility_events_reason is None
        # Projection must also check owner independently of intake.
        assert link_view(replace(link, v4=replace(parsed, owner=owner)), NOW)[
            "flexibility_events"] is None


@pytest.mark.parametrize("change", [
    {"export_during_events": 1}, {"export_during_events": "true"},
    {"export_enabled_at": None}, {"export_enabled_at": "2026-10-24T11:00:00"},
    {"export_enabled_at": "invalid"}, {"export_enabled_at": 123},
    {"export_ratings_w": {}}, {"export_ratings_w": {"unknown": 1000}},
    *({"export_ratings_w": {
        "i0_" + "x" * 61: value,
    }} for value in (True, "5000", 0, -1, 100001, float("inf"), float("nan"))),
    {"export_ratings_w": {f"i{i}": 1000 for i in range(9)}},
    {"export_enabled_by": CANARY}, {"unexpected": CANARY},
])
def test_invalid_block_is_local(change):
    raw, topology = facts()
    before = parse_snapshot_v4(raw, topology)
    raw["flexibility_events"].update(change)
    parsed = parse_snapshot_v4(raw, topology)
    assert parsed.flexibility_events is None
    assert parsed.flexibility_events_reason == "invalid_flexibility_events"
    assert replace(parsed, flexibility_events=before.flexibility_events,
                   flexibility_events_reason=None) == before
    view = link_view(IntegrationLink(None, None, False, v4=parsed), NOW)
    assert view["flexibility_events"] is None
    assert CANARY not in json.dumps(view)


@pytest.mark.parametrize("block", [[], "invalid", True, 123])
def test_malformed_block_never_raises(block):
    raw, topology = facts()
    raw["flexibility_events"] = block
    parsed = parse_snapshot_v4(raw, topology)
    assert parsed.flexibility_events_reason == "invalid_flexibility_events"


@pytest.mark.parametrize("explicit_null", [False, True])
def test_missing_block_is_older_integration(explicit_null):
    raw, topology = facts()
    raw.pop("flexibility_events")
    if explicit_null:
        raw["flexibility_events"] = None
    parsed = parse_snapshot_v4(raw, topology)
    assert parsed.flexibility_events is None
    assert parsed.flexibility_events_reason is None


def test_off_without_ratings_is_valid():
    raw, topology = facts()
    raw["flexibility_events"] = {
        "export_during_events": False, "export_enabled_at": None, "export_ratings_w": {},
    }
    assert parse_snapshot_v4(raw, topology).flexibility_events is not None
