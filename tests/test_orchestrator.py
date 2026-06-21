"""Tests for the proactive orchestrator (decision/audit skeleton)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.habits import HabitContext
from ha_spark.energy.orchestrator import Decision, decide_outcome, decisions_for
from ha_spark.energy.publish import publish_predictions
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"


def test_decide_outcome_is_advisory_in_every_mode() -> None:
    # ponytail: no action actuates yet, so the gate is advisory regardless of mode.
    for action in ("suggest_away_context", "reduce_overnight_charge"):
        for mode in ("off", "simulate", "on"):
            assert decide_outcome(action, mode) == "advisory"


def test_decisions_for_away_period() -> None:
    ctx = HabitContext(
        target_date=date(2026, 6, 22),
        predicted_occupancy=0.1,
        away_active=True,
        learned_away_factor=0.3,
    )
    decisions = decisions_for(ctx, "simulate")
    assert [d.action for d in decisions] == ["reduce_overnight_charge"]
    d = decisions[0]
    assert d.mode == "simulate"
    assert d.outcome == "advisory"
    assert d.confidence == 0.9  # learned factor present -> high confidence


def test_decisions_for_low_occupancy_no_away() -> None:
    ctx = HabitContext(
        target_date=date(2026, 6, 22),
        predicted_occupancy=0.05,
        away_active=False,
        learned_away_factor=None,
    )
    decisions = decisions_for(ctx, "on")
    assert [d.action for d in decisions] == ["suggest_away_context"]
    assert decisions[0].outcome == "advisory"
    assert decisions[0].mode == "on"


@respx.mock
async def test_publish_predictions_pushes_sensor(tmp_path: Path) -> None:
    route = respx.post(f"{BASE}/states/sensor.ha_spark_predictions").mock(
        return_value=httpx.Response(200, json={})
    )
    decisions = [
        Decision(
            "reduce_overnight_charge", 0.9, "an away period is active", "simulate", "advisory"
        ),
    ]
    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    async with HomeAssistantRest(BASE, "tok") as rest:
        await publish_predictions(rest, decisions, settings)

    assert route.called
    body = json.loads(respx.calls.last.request.content)
    assert body["state"] == "1"
    assert body["attributes"]["proactive_mode"] == "simulate"
    assert body["attributes"]["predictions"][0]["action"] == "reduce_overnight_charge"
    assert body["attributes"]["predictions"][0]["outcome"] == "advisory"
