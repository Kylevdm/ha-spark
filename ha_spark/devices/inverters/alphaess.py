"""AlphaESS inverter driver: charge window + stop-SOC via the
``alphaess.setbatterycharge`` service.

No settable rate -> the supply guard stays dormant for this inverter.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ha_spark.devices.base import Capability, HoldValidation, fmt_hhmm
from ha_spark.devices.execution import (
    ExecutionContext,
    Ownership,
    device_action,
    execution_context,
)
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.devices.registry import register
from ha_spark.energy.models import ChargeIntent

if TYPE_CHECKING:  # avoid an import cycle: config imports devices.base at runtime
    from ha_spark.config import DeviceConfig, Settings
    from ha_spark.ha.rest import HomeAssistantRest


@register("alphaess")
class AlphaESSDevice:
    """AlphaESS: charge window + stop-SOC via the alphaess.setbatterycharge service.

    No settable rate -> the supply guard stays dormant for this inverter.
    """

    capabilities = frozenset({Capability.CHARGE_WINDOW})
    # Transitional `Charger`-protocol compat (`chargers.charger_for`);
    # superseded by `Capability.CHARGE_RATE in capabilities` in Task 6.
    supports_live_rate = False

    def __init__(
        self, config: DeviceConfig, settings: Settings, rest: HomeAssistantRest,
        *, execution: ExecutionContext | None = None,
    ) -> None:
        self._execution = execution or execution_context()
        self._coordinator = self._execution.coordinator_for(config, settings)
        self._config = config
        self._settings = settings
        self._rest = rest

    async def hold_validation(self) -> HoldValidation:
        return HoldValidation({}, "unreadable", None, "no validated hold surface (#196)")

    async def resident_diagnostic(self) -> dict[str, object]:
        return {"charge_window": "unknown", "charge_current_a": "unknown",
                "power_switch": "unknown"}

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return 0.0  # no rate control; the inverter self-regulates to the SOC target

    async def set_charge_rate(self, watts: float) -> ActionLine:
        return ActionLine("[SKIP] AlphaESS has no settable charge rate", ActionOutcome.UNVERIFIED)

    async def read_charge_rate(self) -> float:
        return 0.0

    async def reconcile_holds(self, intent: ChargeIntent, now: datetime) -> list[ActionLine]:
        """No hold surface: AlphaESS exposes no whole-inverter enable to drive."""
        return []

    async def write_safe_state(self) -> list[ActionLine]:
        """No hold surface, so nothing is left steered when ha-spark lets go."""
        return []

    async def write_safe_window(self) -> list[ActionLine]:
        """No hold surface, so no shutdown keeps one."""
        return []

    @device_action(Ownership.CHARGING)
    async def apply(self, intent: ChargeIntent) -> list[ActionLine]:
        stop_soc = round(intent.target_soc_pct)
        desc = (
            f"charge to {stop_soc}% in window "
            f"{fmt_hhmm(intent.window_start)}-{fmt_hhmm(intent.window_end)}"
        )
        mode = self._coordinator.mode
        if mode.startswith("refuse:"):
            return [ActionLine(
                f"[REFUSED] {mode.removeprefix('refuse:')}", ActionOutcome.REFUSED
            )]
        if mode == "on" and not intent.soc.ok:
            return [ActionLine(f"[BLOCKED] {intent.soc.reason}; not {desc}", ActionOutcome.BLOCKED)]
        if mode == "simulate":
            return [ActionLine(f"[SIMULATE] would {desc}", ActionOutcome.SIMULATED)]
        if mode in ("off", "observe"):
            return [ActionLine(f"[{mode.upper()}] computed: {desc}", ActionOutcome.SIMULATED)]
        try:
            # Field names checked against the AlphaESS integration's services.yaml.
            self._coordinator.confirm_write()
            await self._rest.call_service(
                "alphaess",
                "setbatterycharge",
                {
                    "serial": self._settings.alphaess_serial,
                    "enabled": True,
                    "cp1start": fmt_hhmm(intent.window_start),
                    "cp1end": fmt_hhmm(intent.window_end),
                    "chargestopsoc": stop_soc,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return [ActionLine(f"[FAILED] {desc}: {exc!r}", ActionOutcome.FAILED)]
        # No read-back exists on this driver: VERIFIED keeps today's "[APPLIED]
        # is success" baseline (#410). AlphaESS never qualifies for an active
        # operating mode (#409), so this never counts as activation evidence.
        return [ActionLine(f"[APPLIED] {desc}", ActionOutcome.VERIFIED)]
