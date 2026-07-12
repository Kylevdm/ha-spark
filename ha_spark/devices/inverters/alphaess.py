"""AlphaESS inverter driver: charge window + stop-SOC via the
``alphaess.setbatterycharge`` service.

No settable rate -> the supply guard stays dormant for this inverter.
"""
from __future__ import annotations

from ha_spark.config import DeviceConfig, Settings
from ha_spark.devices.base import Capability, effective_mode, fmt_hhmm
from ha_spark.devices.registry import register
from ha_spark.energy.models import ChargeIntent
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

    def __init__(self, config: DeviceConfig, settings: Settings, rest: HomeAssistantRest) -> None:
        self._config = config
        self._settings = settings
        self._rest = rest

    def planned_rate_w(self, intent: ChargeIntent) -> float:
        return 0.0  # no rate control; the inverter self-regulates to the SOC target

    async def set_charge_rate(self, watts: float) -> str:
        return "[SKIP] AlphaESS has no settable charge rate"

    async def read_charge_rate(self) -> float:
        return 0.0

    async def apply(self, intent: ChargeIntent) -> list[str]:
        stop_soc = round(intent.target_soc_pct)
        desc = (
            f"charge to {stop_soc}% in window "
            f"{fmt_hhmm(intent.window_start)}-{fmt_hhmm(intent.window_end)}"
        )
        mode = effective_mode(self._config.control, self._settings.proactive_mode)
        if mode == "on" and not intent.soc_valid:
            return [f"[BLOCKED] SoC unreadable; not {desc}"]
        if mode == "simulate":
            return [f"[SIMULATE] would {desc}"]
        if mode in ("off", "observe"):
            return [f"[{mode.upper()}] computed: {desc}"]
        try:
            # VERIFY before shipping: confirm the alphaess.setbatterycharge field
            # names (serial, enabled, cp1start, cp1end, chargeStopSOC) against the
            # integration's services.yaml on the tester's box (Developer Tools ->
            # Services, or the CharlesGillanders integration repo) — unverified.
            await self._rest.call_service(
                "alphaess",
                "setbatterycharge",
                {
                    "serial": self._settings.alphaess_serial,
                    "enabled": True,
                    "cp1start": fmt_hhmm(intent.window_start),
                    "cp1end": fmt_hhmm(intent.window_end),
                    "chargeStopSOC": stop_soc,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return [f"[FAILED] {desc}: {exc!r}"]
        return [f"[APPLIED] {desc}"]
