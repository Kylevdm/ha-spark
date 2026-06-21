"""Tests for the NL-copilot grounding (Phase 5)."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest

from ha_spark import copilot
from ha_spark.config import Settings
from ha_spark.copilot import COPILOT_SYSTEM, build_grounding, grounded_system_prompt
from ha_spark.energy.models import ChargeIntent, ChargePlan

REST = object()  # only forwarded to gather_inputs (mocked)


def _plan() -> ChargePlan:
    return ChargePlan(
        soc_now=42, capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=5.9,
        deficit_kwh=12.8, buffer_pct=20.0, required_kwh=12.8,
        target_soc=77, window_hours=6.0,
        ev_charging=False, ha_template_needed=None,
        charge_intent=ChargeIntent(
            target_soc_pct=77, soc_now=42, window_start=time(23, 30), window_end=time(5, 30)
        ),
    )


async def test_build_grounding_renders_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_gather(settings: Settings, rest: Any) -> tuple[object, object, str]:
        return object(), object(), "slot profile (14d hourly house stats)"

    monkeypatch.setattr(copilot, "gather_inputs", fake_gather)
    monkeypatch.setattr(copilot, "compute_plan", lambda inputs, cfg: _plan())

    grounding = await build_grounding(Settings(), REST)  # type: ignore[arg-type]
    assert grounding is not None
    assert "Charge plan:" in grounding
    assert "Home load forecast 24.20 kWh" in grounding
    assert "slot profile" in grounding


async def test_build_grounding_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Settings, rest: Any) -> tuple[object, object, str]:
        raise RuntimeError("HA down")

    monkeypatch.setattr(copilot, "gather_inputs", boom)
    assert await build_grounding(Settings(), REST) is None  # type: ignore[arg-type]


def test_grounded_system_prompt_includes_facts() -> None:
    prompt = grounded_system_prompt("Charge plan:\n  SoC now 42%")
    assert COPILOT_SYSTEM in prompt
    assert "Current plan and live state:" in prompt
    assert "SoC now 42%" in prompt


def test_grounded_system_prompt_handles_missing_plan() -> None:
    prompt = grounded_system_prompt(None)
    assert COPILOT_SYSTEM in prompt
    assert "unavailable" in prompt
