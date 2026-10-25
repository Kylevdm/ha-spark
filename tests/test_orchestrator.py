"""Tests for the proactive orchestrator (decision/audit skeleton)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ha_spark.agent import tools
from ha_spark.config import Settings
from ha_spark.energy.habits import HabitContext
from ha_spark.energy.orchestrator import Decision, decide_outcome, decisions_for


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


async def test_agent_predictions_preserve_advisory_decisions(tmp_path: Path, monkeypatch) -> None:
    decisions = [
        Decision(
            "reduce_overnight_charge", 0.9, "an away period is active", "simulate", "advisory"
        ),
    ]
    settings = Settings(db_path=str(tmp_path / "ha_spark.db"))
    async def orchestrate(_settings):
        return decisions

    monkeypatch.setattr(tools, "orchestrate", orchestrate)
    result = await tools.get_predictions(settings)
    assert result.decisions == [{
        "action": "reduce_overnight_charge", "confidence": 0.9,
        "reason": "an away period is active", "mode": "simulate", "outcome": "advisory",
    }]
