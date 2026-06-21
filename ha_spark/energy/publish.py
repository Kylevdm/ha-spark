"""Publish the computed charge plan to HA as sensor.ha_spark_* states.

ha-spark otherwise only logs its calculations and writes its own SQLite DB —
nothing is visible in Home Assistant. This pushes the key numbers via
``POST /api/states`` so they show up as ordinary sensors. Pushed states are
not backed by an integration, so HA forgets them on restart; the last
payload is cached to disk and replayed by :func:`republish_last` so a
restarted daemon doesn't leave them ``unknown`` until the next scheduled run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.orchestrator import Decision
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

Entity = tuple[str, str, dict[str, Any]]


def _cache_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / "ha_spark_published.json"


def _entities(plan: ChargePlan, settings: Settings) -> list[Entity]:
    """Map a computed plan to the (entity_id, state, attributes) to publish."""
    entities: list[Entity] = [
        (
            "sensor.ha_spark_charge_needed_kwh",
            f"{plan.required_kwh:.2f}",
            {
                "friendly_name": "ha-spark charge needed",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_target_soc",
            f"{plan.target_soc:.0f}",
            {
                "friendly_name": "ha-spark target SoC",
                "unit_of_measurement": "%",
                "device_class": "battery",
            },
        ),
        (
            "sensor.ha_spark_soc_now",
            f"{plan.soc_now:.0f}",
            {
                "friendly_name": "ha-spark SoC now",
                "unit_of_measurement": "%",
                "device_class": "battery",
            },
        ),
        (
            "sensor.ha_spark_overnight_current",
            f"{plan.overnight_current_a:.1f}",
            {
                "friendly_name": "ha-spark overnight charge current",
                "unit_of_measurement": "A",
                "device_class": "current",
            },
        ),
        (
            "sensor.ha_spark_forecast_load_kwh",
            f"{plan.load_kwh:.2f}",
            {
                "friendly_name": "ha-spark forecast load",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_solar_forecast_kwh",
            f"{plan.effective_solar_kwh:.2f}",
            {
                "friendly_name": "ha-spark solar forecast",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_deficit_kwh",
            f"{plan.deficit_kwh:.2f}",
            {
                "friendly_name": "ha-spark deficit",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
            },
        ),
        (
            "sensor.ha_spark_plan_status",
            plan.model,
            {
                "friendly_name": "ha-spark plan status",
                "strategy": plan.strategy,
                "proactive_mode": settings.proactive_mode,
                "soc_valid": plan.soc_valid,
            },
        ),
        (
            "sensor.ha_spark_last_run",
            datetime.now(UTC).isoformat(),
            {
                "friendly_name": "ha-spark last run",
                "device_class": "timestamp",
            },
        ),
    ]
    if plan.planned_cost is not None:
        entities.append(
            (
                "sensor.ha_spark_planned_cost",
                f"{plan.planned_cost:.2f}",
                {
                    "friendly_name": "ha-spark planned cost",
                    "unit_of_measurement": "GBP",
                    "device_class": "monetary",
                },
            )
        )
    if plan.baseline_cost is not None:
        entities.append(
            (
                "sensor.ha_spark_baseline_cost",
                f"{plan.baseline_cost:.2f}",
                {
                    "friendly_name": "ha-spark baseline cost (no battery)",
                    "unit_of_measurement": "GBP",
                    "device_class": "monetary",
                },
            )
        )
    return entities


async def _push(rest: HomeAssistantRest, entities: list[Entity]) -> None:
    for entity_id, state, attributes in entities:
        try:
            await rest.set_state(entity_id, state, attributes)
        except Exception:
            log.warning("Publishing %s failed", entity_id, exc_info=True)


async def publish_plan(rest: HomeAssistantRest, plan: ChargePlan, settings: Settings) -> None:
    """Push the plan's computed numbers as sensor.ha_spark_* states (best-effort)."""
    entities = _entities(plan, settings)
    await _push(rest, entities)
    try:
        path = _cache_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entities), encoding="utf-8")
    except OSError:
        log.warning("Caching published states failed", exc_info=True)


async def publish_predictions(
    rest: HomeAssistantRest, decisions: list[Decision], settings: Settings
) -> None:
    """Surface the orchestrator's decisions as sensor.ha_spark_predictions (best-effort)."""
    attrs: dict[str, Any] = {
        "friendly_name": "ha-spark predictions",
        "proactive_mode": settings.proactive_mode,
        "predictions": [
            {
                "action": d.action,
                "confidence": round(d.confidence, 2),
                "reason": d.reason,
                "outcome": d.outcome,
            }
            for d in decisions
        ],
    }
    await _push(rest, [("sensor.ha_spark_predictions", str(len(decisions)), attrs)])


async def republish_last(rest: HomeAssistantRest, settings: Settings) -> None:
    """Re-push the last published payload (no-op if none is cached)."""
    path = _cache_path(settings)
    if not path.is_file():
        return
    try:
        entities: list[Entity] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Reading cached published states failed", exc_info=True)
        return
    await _push(rest, [(e[0], e[1], e[2]) for e in entities])
