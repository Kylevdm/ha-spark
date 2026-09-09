"""The proactive orchestrator: the single chokepoint between the habit API and
acting on its predictions.

The standing design decision is one decision path that always computes
``predict_actions(context)``, logs the predictions, and — gated by the single
``PROACTIVE_MODE`` flag — either executes them as real HA service calls or logs
them as "simulated", with the same code either way.

This is the decision/audit *skeleton*. The current predicted actions
(``suggest_away_context``, ``reduce_overnight_charge``) are advisory strings
with no HA service mapping, so every decision is ``advisory`` for now. The seam
where actuation hangs off is :func:`decide_outcome` — see its docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ha_spark.config import Settings
from ha_spark.energy import habits
from ha_spark.energy.context import ContextStore
from ha_spark.energy.eval import actual_kwh_by_date
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.ledger import ForecastLedger
from ha_spark.ha.statistics import statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Decision:
    """One audited decision: the prediction plus what the orchestrator did with it."""

    action: str
    confidence: float
    reason: str
    mode: str  # off | simulate | on
    outcome: str  # advisory | simulated | executed


def decide_outcome(action: str, mode: str) -> str:
    """Outcome for one predicted action under the proactive ``mode``.

    ponytail: no predicted action actuates an HA service yet, so the outcome is
    always ``advisory`` regardless of mode. When an action becomes actuating,
    gate it here — return ``executed`` when ``mode == "on"`` else ``simulated``
    — and have :func:`orchestrate` perform the ``call_service`` only for
    ``executed``. Same decision path; flipping the flag is the only change.
    Upgrade path: a config field naming the entity/service to actuate.
    """
    return "advisory"


def decisions_for(ctx: habits.HabitContext, mode: str) -> list[Decision]:
    """Pure: map the habit API's predictions to audited decisions (no IO)."""
    return [
        Decision(p.action, p.confidence, p.reason, mode, decide_outcome(p.action, mode))
        for p in habits.predict_actions(ctx)
    ]


async def _gather_context(settings: Settings) -> habits.HabitContext:
    """Assemble tomorrow's HabitContext from recorded history (occupancy, away
    facts, learned away factor) — the same inputs ``ha-spark learn-factors`` reports.
    """
    tz = load_timezone(settings.timezone)
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    since = datetime.now(UTC) - timedelta(days=settings.profile_history_days)

    async with ForecastLedger(settings.db_path) as ledger:
        occ_samples = await ledger.signal_history("occupancy_home_frac", since)

    away_dates: set[date] = set()
    async with ContextStore(settings.db_path) as store:
        active = await store.active_on(tomorrow)
        for e in await store.list_all():
            if e.kind != "away":
                continue
            d = e.start_date
            while d <= e.end_date:
                away_dates.add(d)
                d += timedelta(days=1)

    # The learned away factor only sharpens an away prediction; if the history
    # fetch fails, fall back to None so predictions still fire (old behavior).
    factor: float | None = None
    try:
        rows = await statistics_during_period(
            settings.ha_websocket_url,
            settings.auth_token,
            settings.consumption_energy_entity,
            since,
            period="day",
            timeout=settings.ha_timeout,
        )
        factor, _ = habits.learn_away_factor(actual_kwh_by_date(rows), away_dates)
    except Exception:
        log.warning("Learned away-factor fetch failed; using configured default", exc_info=True)

    return habits.HabitContext(
        target_date=tomorrow,
        predicted_occupancy=habits.predict_occupancy(occ_samples, tomorrow, tz),
        away_active=any(e.kind == "away" for e in active),
        learned_away_factor=factor,
    )


async def orchestrate(settings: Settings) -> list[Decision]:
    """Request the habit API's predictions for tomorrow, decide + log each, return them.

    Nothing is executed yet (every outcome is ``advisory``); this is the seam
    later proactivity hangs off. Honors ``settings.proactive_mode`` in the audit.
    """
    ctx = await _gather_context(settings)
    decisions = decisions_for(ctx, settings.proactive_mode)
    for d in decisions:
        log.info(
            "Proactive decision [mode=%s -> %s] %s (%.0f%% confidence): %s",
            d.mode,
            d.outcome,
            d.action,
            d.confidence * 100,
            d.reason,
        )
    return decisions
