"""Live supply guard: keep whole-house AC draw under the supply limit.

While the battery is timed-charging, a 7 kW EV dispatch plus the house can push
total supply draw past what the main fuse should carry. Each daemon tick the
guard reads the whole-house draw (``grid_power_entity``, W) and the battery's
charge-rate setpoint (W), then resizes the setpoint to fit the remaining
headroom — shedding when over ``supply_max_current_a``, restoring toward the
plan's target as headroom returns. Writes go through the active ``Charger``, so
PROACTIVE_MODE gating, failure isolation, and read-back verification apply.

Reasoning is in watts throughout: power balances directly across the AC<->DC
inverter, so we never mix the battery's DC charge amps with the AC supply limit.
The battery's contribution to the measured ``supply_w`` is its charge power, and
the fuse ceiling is ``limit_a * supply_voltage_v``.

The guard only ever moves the setpoint within ``[0, target]``; with no target
above zero it cannot start a charge. Disabled while ``grid_power_entity`` is
empty, and dormant for inverters without a settable charge rate.
"""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy.chargers import charger_for
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Setpoint deltas under this (W) are noise; don't churn writes for them.
_MIN_DELTA_W = 200.0


def throttled_rate_w(
    supply_w: float,
    setpoint_w: float,
    target_w: float,
    *,
    limit_a: float,
    supply_voltage_v: float,
) -> float:
    """Charge power (W) that fits the supply limit, capped at the target.

    Power balances directly DC<->AC, so we never mix DC amps with AC amps: the
    measured draw includes the battery's own charging (~= its DC charge power),
    so subtract it to get the other load, then grant the battery the remaining
    headroom under ``limit_a * supply_voltage_v``.
    """
    other_load_w = supply_w - setpoint_w
    limit_w = limit_a * supply_voltage_v
    return max(0.0, min(target_w, limit_w - other_load_w))


class SupplyGuard:
    """Per-tick watcher that resizes the charge-rate setpoint to fit supply."""

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest
        self._charger = charger_for(settings, rest)

    async def tick(self, target_w: float) -> str | None:
        """One guard pass; returns the action line if a resize was applied."""
        s = self._settings
        try:
            supply_w = float((await self._rest.get_state(s.grid_power_entity)).state)
            setpoint_w = await self._charger.read_charge_rate()
        except Exception as exc:  # noqa: BLE001 - never throttle on bad data
            log.warning("Supply guard: read failed (%s); skipping", exc)
            return None

        wanted_w = throttled_rate_w(
            supply_w,
            setpoint_w,
            target_w,
            limit_a=s.supply_max_current_a,
            supply_voltage_v=s.supply_voltage_v,
        )
        if abs(wanted_w - setpoint_w) < _MIN_DELTA_W:
            return None
        return await self._charger.set_charge_rate(wanted_w)
