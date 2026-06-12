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
            if mode == "on" and not plan.soc_valid:
                # A dead SoC sensor makes soc_now default to 0, which would
                # command a max-size charge — never act on it.
                line = f"[BLOCKED] SoC unreadable; not applying: {action.description}"
                log.warning(line)
            else:
                line = await self.apply_action(action)
            lines.append(line)
        return lines

    async def apply_action(self, action: ChargeAction) -> str:
        """Apply one action per PROACTIVE_MODE (no SoC gate — callers decide)."""
        mode = self._settings.proactive_mode
        if mode == "on":
            line = await self._execute(action)
        elif mode == "simulate":
            line = f"[SIMULATE] would {action.description}"
        else:  # "off"
            line = f"[OFF] computed: {action.description}"
        if mode != "off":
            log.info(line)
        return line

    async def _execute(self, action: ChargeAction) -> str:
        """One real HA write — only reached when PROACTIVE_MODE == 'on'.

        Failures are isolated per action (a failed write must not stop the
        rest of the plan), and each write is read back to confirm the entity
        actually took the value — HA accepts service calls the device may
        silently reject.
        """
        try:
            if action.kind == "set_charge_current" and action.current_a is not None:
                entity = self._settings.charge_current_entity
                await self._rest.call_service(
                    "number", "set_value", {"entity_id": entity, "value": action.current_a}
                )
                mismatch = await self._read_back_current(entity, action.current_a)
            elif action.kind == "stop_discharge":
                entity = self._settings.inverter_power_switch_entity
                await self._rest.call_service(
                    "select", "select_option", {"entity_id": entity, "option": "Off"}
                )
                mismatch = await self._read_back_option(entity, "Off")
            else:
                return f"[SKIPPED] unknown action kind {action.kind!r}: {action.description}"
        except Exception as exc:  # noqa: BLE001 - isolate failures per action
            line = f"[FAILED] {action.description}: {exc!r}"
            log.error(line)
            return line
        if mismatch:
            line = f"[WARNING] {action.description}, but {mismatch}"
            log.warning(line)
            return line
        return f"[APPLIED] {action.description}"

    async def _read_back_current(self, entity: str, wanted: float) -> str | None:
        """Mismatch description if ``entity`` did not take ``wanted`` amps."""
        try:
            state = await self._rest.get_state(entity)
            got = float(state.state)
        except Exception as exc:  # noqa: BLE001 - the write may still have landed
            return f"read-back failed: {exc!r}"
        if abs(got - wanted) > 0.5:
            return f"read back {got:g} A (wanted {wanted:g} A)"
        return None

    async def _read_back_option(self, entity: str, wanted: str) -> str | None:
        """Mismatch description if ``entity`` is not in state ``wanted``."""
        try:
            state = await self._rest.get_state(entity)
        except Exception as exc:  # noqa: BLE001 - the write may still have landed
            return f"read-back failed: {exc!r}"
        if str(state.state).lower() != wanted.lower():
            return f"read back {state.state!r} (wanted {wanted!r})"
        return None
