"""Tests for the NL-copilot grounding (Phase 5)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

import pytest

from ha_spark import copilot
from ha_spark.config import Settings
from ha_spark.copilot import COPILOT_SYSTEM, build_grounding, grounded_system_prompt
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.energy.plan_run import PlanRun
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )

REST = object()  # only forwarded to current_plan (mocked)


def _plan() -> ChargePlan:
    return ChargePlan(
        soc=_soc(42), capacity_kwh=26.88, solar_kwh=8.75, effective_solar_kwh=8.75,
        load_kwh=24.2, cheap_covered_kwh=0.0, usable_now_kwh=5.9,
        deficit_kwh=12.8, buffer_pct=20.0, required_kwh=12.8,
        target_soc=77, window_hours=6.0,
        ev_charging=False, ha_template_needed=None,
        charge_intent=ChargeIntent(
            target_soc_pct=77, soc=_soc(42), window_start=time(23, 30), window_end=time(5, 30)
        ),
    )


async def test_build_grounding_renders_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_current_plan(settings: Settings, rest: Any) -> PlanRun:
        return PlanRun(
            plan=_plan(), inputs=object(), cfg=object(), schedule=object(),  # type: ignore[arg-type]
            load_source="slot profile (14d hourly house stats)",
        )

    monkeypatch.setattr(copilot, "current_plan", fake_current_plan)

    grounding = await build_grounding(Settings(), REST)  # type: ignore[arg-type]
    assert grounding is not None
    assert "Charge plan:" in grounding
    assert "Home load forecast 24.20 kWh" in grounding
    assert "slot profile" in grounding


async def test_build_grounding_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Settings, rest: Any) -> PlanRun:
        raise RuntimeError("HA down")

    monkeypatch.setattr(copilot, "current_plan", boom)
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
