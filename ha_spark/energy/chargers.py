"""Charger abstraction: turn ChargeActions into (simulated or real) HA writes.

v1 ships ``SolisCharger``; multi-inverter / rectifier support is just another
``Charger`` implementation. ``PROACTIVE_MODE`` gates side effects:
``simulate`` -> log intended writes only; ``on`` -> real ``call_service``;
``off`` -> compute only.
"""

from __future__ import annotations

from typing import Protocol

from ha_spark.config import Settings
from ha_spark.energy.models import ChargeAction, ChargePlan
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)


class Charger(Protocol):
    """Applies a :class:`ChargePlan`; returns human-readable action lines."""

    async def apply(self, plan: ChargePlan) -> list[str]: ...


class SolisCharger:
    """Maps charge actions to Solis number/select entities."""

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest

    async def apply(self, plan: ChargePlan) -> list[str]:
        mode = self._settings.proactive_mode
        lines: list[str] = []
        for action in plan.actions:
            if mode == "on":
                await self._execute(action)
                line = f"[APPLIED] {action.description}"
            elif mode == "simulate":
                line = f"[SIMULATE] would {action.description}"
            else:  # "off"
                line = f"[OFF] computed: {action.description}"
            if mode != "off":
                log.info(line)
            lines.append(line)
        return lines

    async def _execute(self, action: ChargeAction) -> None:
        """Real HA writes — only reached when PROACTIVE_MODE == 'on' (not v1)."""
        if action.kind == "set_charge_current" and action.current_a is not None:
            await self._rest.call_service(
                "number",
                "set_value",
                {"entity_id": self._settings.charge_current_entity, "value": action.current_a},
            )
        elif action.kind == "stop_discharge":
            await self._rest.call_service(
                "select",
                "select_option",
                {"entity_id": self._settings.inverter_power_switch_entity, "option": "Off"},
            )
