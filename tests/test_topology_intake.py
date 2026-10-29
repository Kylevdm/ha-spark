"""v3 field-local acceptance at the authenticated heartbeat seam."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.energy.topology_record import load_topology_record
from tests.test_integration_route import discovery as discovery
from tests.test_integration_route import state as state

FIXTURES = Path(__file__).parent / "fixtures/contract"


def body():
    payload = json.loads((FIXTURES / "site_snapshot_max.json").read_text())
    payload["topology"]["revision"] = 1
    return payload


def post(state, discovery, payload):
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", content=json.dumps(payload), headers={
            "Authorization": f"Bearer {discovery.auth_token}",
        })
    assert response.status_code == 200
    return response


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("case", ["v1", "v2", "absent", "unconfigured", "invalid"])
def test_absence_table(state, discovery, accepted, case, caplog):
    payload = body()
    if accepted:
        post(state, discovery, payload)
    if case in ("v1", "v2"):
        payload["api_version"] = int(case[-1])
    elif case == "absent":
        payload.pop("topology")
    elif case == "unconfigured":
        payload["topology"] = {"configured": False}
    else:
        payload["topology"] = {"configured": True, "revision": 1, "topology": {"CANARY": 1}}
    response = post(state, discovery, payload)
    view = state.integration_link_view()
    assert view["topology_source"] == ("native" if accepted else "legacy")
    assert view["topology_revision"] == (1 if accepted else None)
    if case == "invalid":
        assert view["topology_reason"] == "invalid_topology"
    elif accepted:
        assert view["topology_reason"] == "topology_not_configured"
    assert "CANARY" not in response.text + caplog.text
    assert load_topology_record(state.settings).kind == ("native" if accepted else "legacy")
    if not accepted:
        assert not list(Path(state.settings.db_path).parent.glob("ha_spark_topology*"))


def test_revision_persisted_once_and_replans_once(state, discovery):
    executor = Mock()
    executor.authority_inputs.return_value = None
    # No executor during response projection: only request_replan is relevant here.
    from ha_spark.api.contract import AuthorityInputs
    executor.authority_inputs.return_value = AuthorityInputs(None, False, False)
    state.executor = executor
    payload = body()
    post(state, discovery, payload)
    path = Path(state.settings.db_path).parent / "ha_spark_topology.json"
    before = path.stat().st_mtime_ns
    post(state, discovery, payload)
    assert path.stat().st_mtime_ns == before
    assert executor.request_replan.call_count == 1
    payload["topology"]["revision"] = 2
    post(state, discovery, payload)
    assert executor.request_replan.call_count == 2
    restarted = AppState(state.settings, path.parent / "options.json")
    assert restarted.integration_link.topology.revision == 2
    assert restarted.integration_link.topology.kind == "native"


@pytest.mark.parametrize("block,bad,reason", [
    ("major_loads", [{"id": "unknown", "draw_kw": 1, "reported_at": None}],
     "invalid_major_loads"),
    ("major_loads", [{}] * 13, "invalid_major_loads"),
    ("major_loads", [{"id": "unknown", "draw_kw": 1e999, "reported_at": None}],
     "invalid_major_loads"),
    ("supply_guard", {"limiting": "yes"}, "invalid_supply_guard"),
])
def test_bad_blocks_are_independent(state, discovery, block, bad, reason):
    payload = body()
    payload[block] = bad
    # JSON finite overflow rather than a non-JSON Infinity constant.
    if block == "major_loads" and len(bad) == 1 and bad[0].get("draw_kw") == float("inf"):
        bad[0]["draw_kw"] = "CANARY"
    post(state, discovery, payload)
    link = state.integration_link
    assert link.topology.kind == "native"
    if block == "major_loads":
        assert link.major_loads == () and link.major_loads_reason == reason
        assert link.supply_guard is not None
    else:
        assert link.supply_guard is None and link.supply_guard_reason == reason
        assert len(link.major_loads) == 12


def test_unavailable_stays_closed_until_valid_snapshot(state, discovery, tmp_path):
    (tmp_path / "ha_spark_topology.initialised").touch()
    restarted = AppState(state.settings, tmp_path / "options.json")
    payload = body()
    payload["topology"] = {"configured": False}
    post(restarted, discovery, payload)
    assert restarted.integration_link.topology.kind == "unavailable"
    post(restarted, discovery, body())
    assert restarted.integration_link.topology.kind == "native"


def test_save_failure_not_accepted(state, discovery, monkeypatch):
    def fail(*args):
        raise OSError("CANARY")
    monkeypatch.setattr("ha_spark.api.topology_intake.save_topology_record", fail)
    response = post(state, discovery, body())
    assert state.integration_link.topology.kind == "legacy"
    assert state.integration_link.topology.reason == "topology_save_failed"
    assert "CANARY" not in response.text
