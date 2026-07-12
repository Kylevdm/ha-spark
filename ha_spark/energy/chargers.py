"""Charger adapters: realize a ChargeIntent as (simulated or real) HA writes.

PROACTIVE_MODE gates side effects: ``simulate`` -> log intended writes only;
``on`` -> real ``call_service``; ``off`` -> compute only. Each adapter isolates
per-write failures and reads back each write to confirm the device took it.

Solis moved to ``devices/inverters/solis.py`` (Phase 7); re-exported here for
back-compat. This module — and ``charger_for`` in particular — is superseded
by ``ha_spark.devices.get_device``/``inverter_device`` once Task 6 lands.
"""
from __future__ import annotations

from typing import Protocol

from ha_spark.config import Settings
from ha_spark.devices.inverters.alphaess import AlphaESSDevice
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a  # noqa: F401
from ha_spark.energy.models import ChargeIntent
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

SolisCharger = SolisDevice  # back-compat alias; removed when chargers.py is deleted
AlphaESSCharger = AlphaESSDevice  # back-compat alias; removed when chargers.py is deleted


class Charger(Protocol):
    """Realizes a :class:`ChargeIntent`; returns human-readable action lines."""

    supports_live_rate: bool

    async def apply(self, intent: ChargeIntent) -> list[str]: ...
    async def set_charge_rate(self, watts: float) -> str: ...
    async def read_charge_rate(self) -> float: ...
    def planned_rate_w(self, intent: ChargeIntent) -> float: ...


def charger_for(settings: Settings, rest: HomeAssistantRest) -> Charger:
    """Select the inverter adapter from ``settings.inverter`` (dict dispatch).

    Both drivers now need their synthesized ``DeviceConfig`` (Task 3's
    dual-read shim always produces one); superseded by
    ``devices.inverter_device`` in Task 6.
    """
    config = next(d for d in settings.devices if d.type == "inverter")
    if settings.inverter == "solis":
        return SolisCharger(config, settings, rest)
    return AlphaESSCharger(config, settings, rest)
