"""Tests for the deterministic offline intent parser."""

from __future__ import annotations

from datetime import time
from typing import Any

import pytest

from ha_spark import intent_parser
from ha_spark.config import Settings
from ha_spark.energy.models import ChargeIntent, ChargePlan
from ha_spark.intent_parser import parse_offline

REST = object()  # parse_offline only forwards this to gather_inputs


def _plan(**kw: object) -> ChargePlan:
    base: dict[str, object] = dict(
        soc_now=69, capacity_kwh=26.88, solar_kwh=3.4, effective_solar_kwh=3.4,
        load_kwh=17.7, cheap_covered_kwh=0.0, usable_now_kwh=13.17,
        deficit_kwh=9.23, buffer_pct=20.0, required_kwh=0.0,
        target_soc=69, window_hours=6.0, ev_charging=False,
        ha_template_needed=None,
        charge_intent=ChargeIntent(
            target_soc_pct=69, soc_now=69, window_start=time(23, 30), window_end=time(5, 30)
        ),
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


# --- Phase 6D: offline context extraction + query ---

from datetime import date, timedelta  # noqa: E402

from ha_spark.energy.context import ContextStore  # noqa: E402
from ha_spark.intent_parser import (  # noqa: E402
    answer_context_query,
    extract_context_offline,
    is_context_query,
    mentions_context,
)

_TODAY = date(2026, 6, 13)  # a Saturday


def test_extract_iso_range_away() -> None:
    e = extract_context_offline("i'm away from 2026-07-01 to 2026-07-14", _TODAY)
    assert e is not None
    assert e.kind == "away"
    assert e.start == date(2026, 7, 1)
    assert e.end == date(2026, 7, 14)


def test_extract_next_two_weeks() -> None:
    e = extract_context_offline("on holiday for the next two weeks", _TODAY)
    assert e is not None
    assert e.kind == "away"
    assert e.start == _TODAY
    assert e.end == _TODAY + timedelta(days=13)


def test_extract_fortnight() -> None:
    e = extract_context_offline("away for a fortnight", _TODAY)
    assert e is not None
    assert (e.end - e.start) == timedelta(days=13)


def test_extract_guests_this_weekend() -> None:
    e = extract_context_offline("we have guests this weekend", _TODAY)
    assert e is not None
    assert e.kind == "guests"
    # 2026-06-13 is Saturday; "this weekend" is that Sat + Sun.
    assert e.start == date(2026, 6, 13)
    assert e.end == date(2026, 6, 14)


def test_extract_next_week() -> None:
    e = extract_context_offline("i'm on holiday next week", _TODAY)
    assert e is not None
    assert e.start == date(2026, 6, 15)  # Monday after the 13th
    assert e.end == date(2026, 6, 21)


def test_extract_tomorrow_single_day() -> None:
    e = extract_context_offline("away tomorrow", _TODAY)
    assert e is not None
    assert e.start == e.end == _TODAY + timedelta(days=1)


def test_extract_returns_none_without_kind() -> None:
    assert extract_context_offline("what's the plan for next week", _TODAY) is None


def test_extract_returns_none_without_dates() -> None:
    assert extract_context_offline("i am going on holiday", _TODAY) is None


def test_is_context_query_matches_questions_about_context() -> None:
    assert is_context_query("what do you know about my holidays?")
    assert is_context_query("show upcoming context")
    assert not is_context_query("what's tonight's charge plan?")
    assert not is_context_query("i'm away next week")  # a statement, not a query


def test_mentions_context_prefilter() -> None:
    assert mentions_context("i'm away next week")
    assert mentions_context("guests staying 2026-07-01")
    assert not mentions_context("what's the battery soc")


async def test_answer_context_query_lists_facts(tmp_path: Any) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    async with ContextStore(settings.db_path) as store:
        await store.add("away", date(2026, 7, 1), date(2026, 7, 14), note="Italy")
    out = await answer_context_query(settings, _TODAY)
    assert "away" in out and "Italy" in out and "upcoming" in out


async def test_answer_context_query_empty(tmp_path: Any) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    out = await answer_context_query(settings, _TODAY)
    assert "No context facts" in out
