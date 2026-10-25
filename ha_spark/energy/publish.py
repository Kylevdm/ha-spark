"""Legacy plan payload for GET /api/plan, agent tools and daemon replies."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ha_spark.authority import operating_mode
from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan

Entity = tuple[str, str, dict[str, Any]]
_SOC_DERIVED_SENSORS = frozenset(
    {
        "sensor.ha_spark_charge_needed_kwh",
        "sensor.ha_spark_target_soc",
        "sensor.ha_spark_soc_now",
        "sensor.ha_spark_deficit_kwh",
        "sensor.ha_spark_planned_cost",
        "sensor.ha_spark_baseline_cost",
    }
)


def _hide_untrusted_soc_values(entities: list[Entity]) -> list[Entity]:
    """Replace SoC-derived states with ``unavailable`` when their source is untrusted."""
    return [
        (entity_id, "unavailable" if entity_id in _SOC_DERIVED_SENSORS else state, attributes)
        for entity_id, state, attributes in entities
    ]


def plan_to_payload(plan: ChargePlan, settings: Settings) -> list[Entity]:
    """Map a computed plan to the (entity_id, state, attributes) sensors.

    GET /api/plan, the agent tools and daemon replies use this legacy payload.
    """
    plan_status_attributes: dict[str, Any] = {
        "friendly_name": "ha-spark plan status",
        "strategy": plan.strategy,
        "operating_mode": operating_mode(settings),
        "soc_status": plan.soc.status.value,
        "soc_reason": plan.soc.reason,
    }
    if plan.export_soc_needed_pct is not None and plan.export_soc_needed_at is not None:
        plan_status_attributes["export_soc_needed_pct"] = round(plan.export_soc_needed_pct)
        plan_status_attributes["export_soc_needed_at"] = plan.export_soc_needed_at.isoformat()

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
            plan_status_attributes,
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
    return entities if plan.soc.ok else _hide_untrusted_soc_values(entities)
