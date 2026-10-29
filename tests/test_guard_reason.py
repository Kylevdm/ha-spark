"""The v3 guard block is display-only: a reason, never a planning input.

A limiting guard adds a fixed shortfall sentence to the charge status. The live
allowance never reaches a plan number, and an invalid block adds nothing.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.energy.models import ChargePlan
from tests.test_api import _plan
from tests.test_integration_route import discovery as discovery
from tests.test_integration_route import state as state

FIXTURES = Path(__file__).parent / "fixtures/contract"
GUARD_TEXT = " Charging limited by the supply guard."


@pytest.fixture(autouse=True)
def fixed_heartbeat_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan's charge window must cover the resolved instant, deterministically."""
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))


def body() -> dict[str, object]:
    payload = json.loads((FIXTURES / "site_snapshot_max.json").read_text())
    payload["topology"]["revision"] = 1
    return payload


def charge_plan() -> ChargePlan:
    plan = _plan()
    return replace(plan, charge_intent=replace(
        plan.charge_intent, window_start=time(10), window_end=time(17)))


def guard_block(payload: dict[str, object], allowance_kw: float, limiting: bool = True,
) -> dict[str, object]:
    ids = [c["id"] for c in payload["topology"]["topology"]["connections"]]
    return {
        "limiting": limiting,
        "connections": [{"id": id_, "allowance_kw": allowance_kw} for id_ in ids],
        "restores_left": 47,
        "revision": 1,
    }


def post(state: AppState, discovery, payload: dict[str, object]) -> dict[str, object]:
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", content=json.dumps(payload), headers={
            "Authorization": f"Bearer {discovery.auth_token}",
        })
    assert response.status_code == 200
    return response.json()


def test_limiting_block_appends_the_guard_reason(state, discovery):
    state.set_plan(charge_plan())
    payload = body()
    payload["supply_guard"] = guard_block(payload, 3.0)
    plan = post(state, discovery, payload)["plan"]
    assert plan["status"]["action"] == "charge"
    assert plan["status"]["reason"].endswith(GUARD_TEXT)
    assert plan["charge_needed_kwh"] == 9.6
    assert plan["target_soc_pct"] == 90


@pytest.mark.parametrize("allowance_kw", [0, 60])
def test_allowance_never_reaches_a_plan_number(state, discovery, allowance_kw):
    plan_object = charge_plan()
    state.set_plan(plan_object)
    payload = body()
    payload["supply_guard"] = guard_block(payload, allowance_kw)
    first = post(state, discovery, payload)["plan"]
    payload["supply_guard"] = guard_block(payload, 60 - allowance_kw)
    second = post(state, discovery, payload)["plan"]
    assert first["charge_needed_kwh"] == second["charge_needed_kwh"] == 9.6
    assert first["target_soc_pct"] == second["target_soc_pct"] == 90
    # No replan: the guard block changes nothing the planner consumes.
    assert state.plan is plan_object


@pytest.mark.parametrize("block", [
    {"limiting": "yes"},
    {"limiting": False, "connections": (), "restores_left": 48, "revision": 1},
])
def test_invalid_or_not_limiting_block_gives_no_reason(state, discovery, block):
    state.set_plan(charge_plan())
    payload = body()
    payload["supply_guard"] = block
    plan = post(state, discovery, payload)["plan"]
    assert plan["status"]["action"] == "charge"
    assert GUARD_TEXT not in plan["status"]["reason"]
