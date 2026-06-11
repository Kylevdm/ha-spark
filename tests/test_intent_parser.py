"""Tests for the deterministic offline intent parser."""

from __future__ import annotations

from typing import Any

import pytest

from ha_spark import intent_parser
from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan
from ha_spark.intent_parser import parse_offline

REST = object()  # parse_offline only forwards this to gather_inputs


def _plan(**kw: object) -> ChargePlan:
    base: dict[str, object] = dict(
        soc_now=69, capacity_kwh=26.88, solar_kwh=3.4, effective_solar_kwh=3.4,
        load_kwh=17.7, cheap_covered_kwh=0.0, usable_now_kwh=13.17,
        deficit_kwh=9.23, buffer_pct=20.0, required_kwh=0.0,
        target_soc=69, overnight_current_a=0, window_hours=6.0, ev_charging=False,
        ha_template_needed=None, actions=(),
    )
    base.update(kw)
    return ChargePlan(**base)  # type: ignore[arg-type]


def _patch_plan(monkeypatch: pytest.MonkeyPatch, plan: ChargePlan) -> None:
    async def fake_gather(settings: Settings, rest: Any) -> tuple[object, object, str]:
        return object(), object(), "test"

    monkeypatch.setattr(intent_parser, "gather_inputs", fake_gather)
    monkeypatch.setattr(intent_parser, "compute_plan", lambda inputs, cfg: plan)


async def test_plan_query_returns_full_report(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, _plan())
    result = await parse_offline("what's the charge plan tonight?", Settings(), REST)  # type: ignore[arg-type]
    assert result.matched
    assert "Charge plan:" in result.text
    assert "Solar tomorrow" in result.text


async def test_soc_query_returns_battery_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, _plan())
    result = await parse_offline("what's the battery soc?", Settings(), REST)  # type: ignore[arg-type]
    assert result.matched
    assert result.text == "Battery is at 69% (13.17 kWh usable)."


async def test_soc_query_flags_unreadable_sensor(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, _plan(soc_valid=False))
    result = await parse_offline("battery?", Settings(), REST)  # type: ignore[arg-type]
    assert result.matched
    assert "unreadable" in result.text


async def test_solar_query_returns_forecast(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plan(monkeypatch, _plan(effective_solar_kwh=2.9))
    result = await parse_offline("solar forecast for tomorrow", Settings(), REST)  # type: ignore[arg-type]
    assert result.matched
    assert "3.40 kWh" in result.text
    assert "2.90 kWh after haircut" in result.text


async def test_strategy_query_needs_no_plan() -> None:
    settings = Settings(charge_strategy="fill")
    result = await parse_offline("which strategy is set?", settings, REST)  # type: ignore[arg-type]
    assert result.matched
    assert result.text == "Charge strategy: fill."


async def test_mode_query_needs_no_plan() -> None:
    settings = Settings(proactive_mode="simulate")
    result = await parse_offline("what proactive mode are we in?", settings, REST)  # type: ignore[arg-type]
    assert result.matched
    assert result.text == "PROACTIVE_MODE: simulate."


async def test_window_query_needs_no_plan() -> None:
    settings = Settings(charge_window_start="23:30", charge_window_end="05:30")
    result = await parse_offline("when is the charge window?", settings, REST)  # type: ignore[arg-type]
    # "charge" also matches the plan group, but "window" must win via the
    # settings-only branch before any HA round-trip.
    assert result.matched
    assert result.text == "Charge window: 23:30 – 05:30."


async def test_unmatched_message_returns_help() -> None:
    result = await parse_offline("turn on the kitchen light", Settings(), REST)  # type: ignore[arg-type]
    assert not result.matched
    assert "couldn't match" in result.text
    assert "strategy" in result.text


async def test_plan_failure_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(settings: Settings, rest: Any) -> tuple[object, object, str]:
        raise RuntimeError("HA is down")

    monkeypatch.setattr(intent_parser, "gather_inputs", boom)
    result = await parse_offline("plan?", Settings(), REST)  # type: ignore[arg-type]
    assert result.matched
    assert "Could not compute the charge plan" in result.text
