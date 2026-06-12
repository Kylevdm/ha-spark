"""Live supply guard: keep whole-house AC draw under the supply limit.

While the battery is timed-charging, a 7 kW EV dispatch plus the house can push
total supply draw past what the main fuse should carry. Each daemon tick the
guard reads the whole-house draw (``grid_power_entity``, W) and the battery's
charge-current setpoint, then resizes the setpoint to fit the remaining
headroom — shedding when over ``supply_max_current_a``, restoring toward the
plan's target as headroom returns. Writes go through ``SolisCharger`` so
PROACTIVE_MODE gating, failure isolation, and read-back verification apply.

The guard only ever moves the setpoint within ``[0, target]``; with no target
above zero it cannot start a charge. Disabled while ``grid_power_entity`` is
empty.
"""

from __future__ import annotations

from ha_spark.config import Settings
from ha_spark.energy.chargers import SolisCharger
from ha_spark.energy.models import ChargeAction
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Setpoint deltas under this are noise; don't churn writes for them.
_MIN_DELTA_A = 1.0


def throttled_current(
    supply_w: float,
    setpoint_a: float,
    target_a: float,
    *,
    battery_voltage_v: float,
    limit_a: float,
    supply_voltage_v: float,
) -> float:
    """The DC charge current (A) that fits the supply limit, capped at the target.

    The measured draw includes the battery's own charging (assumed to equal the
    current setpoint while the timed-charge window is active), so subtract it
    to get the non-battery load, then grant the battery whatever AC headroom
    remains under ``limit_a``, converted back to DC amps.
    """
    if supply_voltage_v <= 0 or battery_voltage_v <= 0:
        return target_a
    supply_a = supply_w / supply_voltage_v
    battery_ac_a = setpoint_a * battery_voltage_v / supply_voltage_v
    other_load_a = supply_a - battery_ac_a
    headroom_a = limit_a - other_load_a
    dc_a = headroom_a * supply_voltage_v / battery_voltage_v
    return max(0.0, min(target_a, dc_a))


class SupplyGuard:
    """Per-tick watcher that resizes the charge-current setpoint to fit supply."""

    def __init__(self, settings: Settings, rest: HomeAssistantRest) -> None:
        self._settings = settings
        self._rest = rest
        self._charger = SolisCharger(settings, rest)

    async def tick(self, target_a: float) -> str | None:
        """One guard pass; returns the action line if a resize was applied."""
        s = self._settings
        try:
            supply_w = float((await self._rest.get_state(s.grid_power_entity)).state)
        except Exception as exc:  # noqa: BLE001 - never throttle on bad data
            log.warning("Supply guard: could not read %s (%s); skipping", s.grid_power_entity, exc)
            return None
        try:
            setpoint_a = float((await self._rest.get_state(s.charge_current_entity)).state)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Supply guard: could not read %s (%s); skipping", s.charge_current_entity, exc
            )
            return None

        wanted_a = throttled_current(
            supply_w,
            setpoint_a,
            target_a,
            battery_voltage_v=s.battery_voltage_v,
            limit_a=s.supply_max_current_a,
            supply_voltage_v=s.supply_voltage_v,
        )
        if abs(wanted_a - setpoint_a) < _MIN_DELTA_A:
            return None

        verb = "throttle" if wanted_a < setpoint_a else "restore"
        action = ChargeAction(
            kind="set_charge_current",
            description=(
                f"{verb} charge current {setpoint_a:g} -> {wanted_a:.0f} A "
                f"(supply {supply_w / s.supply_voltage_v:.0f} A, "
                f"limit {s.supply_max_current_a:g} A)"
            ),
            current_a=round(wanted_a),
        )
        return await self._charger.apply_action(action)
