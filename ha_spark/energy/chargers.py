"""Charger adapters: realize a ChargeIntent as (simulated or real) HA writes.

PROACTIVE_MODE gates side effects: ``simulate`` -> log intended writes only;
``on`` -> real ``call_service``; ``off`` -> compute only. Each adapter isolates
per-write failures and reads back each write to confirm the device took it.

Solis moved to ``devices/inverters/solis.py`` (Phase 7); re-exported here for
back-compat. This module — and ``charger_for`` in particular — is superseded
by ``ha_spark.devices.get_device``/``inverter_device`` once Task 6 lands.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import time
from typing import Protocol

from ha_spark.config import Settings
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a  # noqa: F401
from ha_spark.energy.models import ChargeIntent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

SolisCharger = SolisDevice  # back-compat alias; removed when chargers.py is deleted


class Charger(Protocol):
    """Realizes a :class:`ChargeIntent`; returns human-readable action lines."""

    supports_live_rate: bool

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...


def _fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


class AlphaESSCharger:
    """AlphaESS: charge window + stop-SOC via the alphaess.setbatterycharge service.

    No settable rate -> the supply guard stays dormant for this inverter.
    """

    supports_live_rate = False

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
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
            f"{_fmt_hhmm(intent.window_start)}-{_fmt_hhmm(intent.window_end)}"
        )
        mode = self._settings.proactive_mode
        if mode == "on" and not intent.soc_valid:
            return [f"[BLOCKED] SoC unreadable; not {desc}"]
        if mode == "simulate":
            return [f"[SIMULATE] would {desc}"]
        if mode == "off":
            return [f"[OFF] computed: {desc}"]
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
                    "cp1start": _fmt_hhmm(intent.window_start),
                    "cp1end": _fmt_hhmm(intent.window_end),
                    "chargeStopSOC": stop_soc,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return [f"[FAILED] {desc}: {exc!r}"]
        return [f"[APPLIED] {desc}"]


def charger_for(settings: Settings, rest: HomeAssistantRest) -> Charger:
    """Select the inverter adapter from ``settings.inverter`` (dict dispatch).

    Solis now needs its synthesized ``DeviceConfig`` (Task 3's dual-read shim
    always produces one); superseded by ``devices.inverter_device`` in Task 6.
    """
    if settings.inverter == "solis":
        config = next(d for d in settings.devices if d.type == "inverter")
        return SolisCharger(config, settings, rest)
    alphaess: dict[str, Callable[[Settings, HomeAssistantRest], Charger]] = {
        "alphaess": AlphaESSCharger,
    }
    return alphaess[settings.inverter](settings, rest)
