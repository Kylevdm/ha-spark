"""Pure readiness evaluation over the latest qualifying household-local days.

The loader reads existing evidence only. Daily notices persist only the authority
notice flag, before delivery; neither evaluation nor delivery grants control.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict

from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ForecastHistoryRecord
from ha_spark.energy.readiness_ledger import GuardRecord, ReadinessLedger
from ha_spark.energy.readiness_measurements import (
    COMPONENTS,
    REQUIRED,
    DayRecord,
    ReadinessDays,
    day_bounds,
)
from ha_spark.energy.readiness_replay import ReplayResult, replay_day
from ha_spark.ha.notifier import send_notification
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


class ReadinessResult(BaseModel):
    """Matched measurements and explicit gaps behind a recommendation."""

    model_config = ConfigDict(frozen=True)
    verdict: Literal["pass", "fail", "insufficient_evidence"]
    cost_delta: float
    breaches: int
    guard_coverage_pct: float | None
    valid_guard_samples: int
    expected_guard_samples: float
    model_mae: float | None
    median_mae: float | None
    mae_improvement_pct: float | None
    qualifying_days: tuple[date, ...]
    excluded_days: dict[date, tuple[str, ...]]
    reasons: tuple[str, ...]


def evaluate_readiness(
    days: list[DayRecord], guards: list[GuardRecord],
    forecasts: list[ForecastHistoryRecord], settings: Settings,
) -> ReadinessResult:
    """Evaluate recorded evidence only; measured failures outrank insufficiency."""
    excluded: dict[date, tuple[str, ...]] = {}
    candidates: list[tuple[DayRecord, ReplayResult, ForecastHistoryRecord]] = []
    for day in sorted(days, key=lambda d: d.local_date):
        if not day.qualifying:
            excluded[day.local_date] = day.reasons
            continue
        try:
            start, end = day_bounds(day.local_date, day.timezone)
            expected_slots = {start + timedelta(minutes=30 * i)
                              for i in range(int((end - start).total_seconds() / 1800))}
            if (len(day.slots) != len(expected_slots)
                    or {s.slot_start for s in day.slots} != expected_slots):
                raise ValueError("incomplete or duplicate day slots")
            for slot in day.slots:
                if slot.intent is None:
                    raise ValueError("slot record missing")
                if slot.intent.operating_mode != "observe":
                    raise ValueError("not Observe")
                for name in COMPONENTS:
                    component = slot.components.get(name)
                    allowed = {"measured", "hourly_spread"}
                    if name not in REQUIRED:
                        allowed.add("absent")
                    if component is None or component.provenance not in allowed:
                        raise ValueError(f"{name}: measurement provenance invalid")
            day = day.model_copy(update={"slots": tuple(sorted(
                day.slots, key=lambda s: s.slot_start,
            ))})
            replay = replay_day(day)
        except ValueError as exc:
            excluded[day.local_date] = (str(exc),)
            continue
        start, _ = day_bounds(day.local_date, day.timezone)
        driving = [f for f in forecasts if f.drove_plan and f.target_date == day.local_date
                   and f.issued_at < start]
        if not driving:
            excluded[day.local_date] = ("forecast missing",)
            continue
        candidates.append((day, replay, max(driving, key=lambda f: f.issued_at)))
    selected = candidates[-settings.readiness_window_days:]
    insufficient = []
    failures = []
    if len(selected) < settings.readiness_window_days:
        insufficient.append("too few qualifying days")
    cost = sum(r.delta for _, r, _ in selected)
    expected = sum(s.intent.charge_window_fraction * 30 for d, _, _ in selected
                   for s in d.slots if s.intent is not None)
    # Only samples in a recorded in-window slot count, and a duplicated tick
    # cannot manufacture coverage.
    windows = {s.slot_start for d, _, _ in selected for s in d.slots
               if s.intent is not None and s.intent.charge_window_fraction > 0}
    samples = [g for g in guards if g.slot_start in windows]
    valid = len({g.observed_at for g in samples if g.sample_valid})
    coverage = min(100.0, valid / expected * 100) if expected else None
    bounds = [day_bounds(d.local_date, d.timezone) for d, _, _ in selected]
    breaches = sum(g.breach is True for g in guards
                   if any(start <= g.observed_at < end for start, end in bounds))
    if selected and cost > settings.readiness_max_cost_delta:
        failures.append("cost delta above threshold")
    if breaches:
        failures.append("guard breaches")
    if coverage is None or valid == 0:
        insufficient.append("guard evidence missing")
    elif coverage < settings.readiness_min_guard_coverage_pct:
        failures.append("guard coverage below threshold")
    models = {f.model for _, _, f in selected}
    models.update(s.intent.forecast_model for d, _, _ in selected for s in d.slots
                  if s.intent is not None)
    if len(models) > 1:
        insufficient.append("model change")
    median_active = bool(models & {"slots", "median", "baseline"})
    if median_active:
        insufficient.append("median active")
    paired = all(f.median_total_kwh is not None for _, _, f in selected)
    model_mae = median_mae = improvement = None
    if selected:
        actuals = [sum(s.load_kwh - s.components["v2l"].kwh for s in d.slots)
                   for d, _, _ in selected]
        model_mae = sum(abs(f.total_kwh - a) for (_, _, f), a in
                        zip(selected, actuals, strict=True)) / len(selected)
        if paired:
            median_mae = sum(abs(f.median_total_kwh - a) for (_, _, f), a in
                             zip(selected, actuals, strict=True)
                             if f.median_total_kwh is not None) / len(selected)
            if median_mae == 0:
                insufficient.append("zero baseline MAE")
            elif len(models) == 1 and not median_active:
                improvement = (1 - model_mae / median_mae) * 100
                if model_mae > median_mae * (1 - settings.readiness_mae_improvement_pct / 100):
                    failures.append("MAE improvement below threshold")
        else:
            insufficient.append("paired median missing")
    return ReadinessResult(
        verdict="fail" if failures else "insufficient_evidence" if insufficient else "pass",
        cost_delta=cost, breaches=breaches, guard_coverage_pct=coverage,
        valid_guard_samples=valid, expected_guard_samples=expected,
        model_mae=model_mae, median_mae=median_mae, mae_improvement_pct=improvement,
        qualifying_days=tuple(d.local_date for d, _, _ in selected), excluded_days=excluded,
        reasons=tuple(failures + insufficient),
    )


async def load_readiness(settings: Settings) -> ReadinessResult:
    """Read existing snapshots, guard ticks and forecast revisions without DDL."""
    days = await ReadinessDays(settings.db_path).days()
    guards = await ReadinessLedger(settings.db_path).guards()
    forecasts = []
    if days:
        path = await asyncio.to_thread(Path(settings.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'forecasts'"
            ) as cursor:
                exists = await cursor.fetchone()
            async with db.execute("PRAGMA table_info(forecasts)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if exists and "issued_at" in columns:
                async with db.execute(
                    "SELECT issued_at, target_date, model, total_kwh, drove_plan, "
                    "median_total_kwh FROM forecasts ORDER BY julianday(issued_at), id"
                ) as cursor:
                    forecasts = [ForecastHistoryRecord(
                        made_at=datetime.fromisoformat(issued), target_date=date.fromisoformat(day),
                        model=model, total_kwh=total, slots=None, source="ledger",
                        drove_plan=bool(drove), median_total_kwh=median,
                    ) for issued, day, model, total, drove, median in await cursor.fetchall()]
    return evaluate_readiness(days, guards, forecasts, settings)


async def notify_readiness(
    result: ReadinessResult, settings: Settings, rest: HomeAssistantRest, authority: AuthorityStore,
) -> None:
    """At most one notice per passing episode; persistence precedes delivery."""
    try:
        state = authority.state
        if state is None or not authority.known or authority.save_failed:
            return
        if result.verdict == "fail":
            authority.save_readiness_notice(False)
        elif result.verdict == "pass" and not state.readiness_notice.notified:
            authority.save_readiness_notice(True)
            await send_notification(settings, rest, "Optimise readiness", (
                "Observe history clears the readiness bar. "
                "Enabling Optimise remains your decision. "
                f"Cost delta: £{result.cost_delta:.2f}; "
                f"qualifying days: {len(result.qualifying_days)}/{settings.readiness_window_days}; "
                f"guard coverage: {result.guard_coverage_pct or 0:.1f}%; "
                f"breaches: {result.breaches}; "
                f"MAE improvement: {result.mae_improvement_pct or 0:.1f}% "
                f"(model {result.model_mae or 0:.2f}, median {result.median_mae or 0:.2f} kWh)."
            ))
    except Exception:
        log.warning("Readiness notice failed")


async def readiness_tick(
    settings: Settings, now: datetime, evaluated: date | None, authority: AuthorityStore,
) -> date | None:
    """Evaluate once after the latest closed household-local snapshot exists."""
    try:
        closed = now.astimezone(load_timezone(settings.timezone)).date() - timedelta(days=1)
        if evaluated == closed:
            return evaluated
        if closed not in await ReadinessDays(settings.db_path).dates():
            return evaluated
        result = await load_readiness(settings)
        async with HomeAssistantRest(
            base_url=settings.ha_rest_url, token=settings.auth_token,
        ) as rest:
            await notify_readiness(result, settings, rest, authority)
        return closed
    except Exception:
        log.warning("Readiness evaluation failed")
        return evaluated
