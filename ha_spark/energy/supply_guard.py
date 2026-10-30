"""Live supply guard: keep whole-house AC draw under the supply limit.

While the battery is timed-charging, a 7 kW EV dispatch plus the house can push
total supply draw past what the main fuse should carry. Each daemon tick the
guard reads the whole-house draw (``grid_power_entity``, W) and the battery's
charge-rate setpoint (W), then resizes the setpoint to fit the remaining
headroom — shedding when over the effective connection import limit (the legacy
``supply_max_current_a`` at ``supply_voltage_v``, or the accepted native
connection's), restoring toward the
plan's target as headroom returns. Writes go through the active ``Charger``, so
the operating-mode write gate, failure isolation, and read-back verification apply.

Reasoning is in watts throughout: power balances directly across the AC<->DC
inverter, so we never mix the battery's DC charge amps with the AC supply limit.
The battery's contribution to the measured ``supply_w`` is its charge power, and
the fuse ceiling is the connection's effective ``import_kw``; settings outside the
topology envelope narrow it to zero.

The guard only ever moves the setpoint within ``[0, target]``; with no target
above zero it cannot start a charge. Disabled while ``grid_power_entity`` is
empty, and dormant for inverters without a settable charge rate.

With the accepted topology record unavailable (#555) the limit falls back to the
legacy ``supply_max_current_a`` at ``supply_voltage_v`` (both finite and
positive, else no write), and the guard writes 0 A only while a fresh grid
reading exceeds it: a finite W value reported no more than 60 s ago and no more
than 5 s ahead. A missing, stale or future-dated reading writes nothing.
Documented gap: an accepted native connection limit lower than the legacy
setting isn't enforced while the record is unreadable.

An unconfirmed SoC fallback follows the pending-failure rule and blocks
increases while allowing valid reductions. A read-back-confirmed fallback
allows restoration only up to its configured current ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.devices.execution import (
    ExecutionContext,
    Ownership,
    device_action,
    execution_context,
)
from ha_spark.devices.inverters.solis import (
    active_limits,
    charge_voltage_v,
    validated_fallback_current_a,
)
from ha_spark.devices.outcomes import ActionLine
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.topology_record import select_limits
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Setpoint deltas under this (W) are noise; don't churn writes for them.
_MIN_DELTA_W = 200.0
# A grid reading older than this (s), or further ahead, is not fresh (#555).
_FRESH_MAX_AGE_S = 60.0
_FRESH_MAX_AHEAD_S = 5.0


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


def _fresh_over_legacy_limit(
    supply: EntityState, supply_w: float, settings: Settings, now: datetime,
) -> bool:
    """True only for a fresh W reading above the legacy supply limit (#555)."""
    limit_a, voltage_v = settings.supply_max_current_a, settings.supply_voltage_v
    if not all(isfinite(v) and v > 0 for v in (limit_a, voltage_v)):
        return False
    reported = supply.last_reported
    if (
        not isfinite(supply_w)
        or supply.attributes.get("unit_of_measurement") != "W"
        or reported is None
        or reported.utcoffset() is None
    ):
        return False
    age_s = (now - reported).total_seconds()
    if not -_FRESH_MAX_AHEAD_S <= age_s <= _FRESH_MAX_AGE_S:
        return False
    return supply_w > limit_a * voltage_v


class SupplyGuard:
    """Per-tick watcher that resizes the charge-rate setpoint to fit supply."""

    def __init__(
        self, settings: Settings, rest: HomeAssistantRest,
        *, execution: ExecutionContext | None = None,
    ) -> None:
        self._settings = settings
        self._rest = rest
        self._execution = execution or execution_context()
        self._config = next(d for d in settings.devices if d.type == "inverter")
        self._coordinator = self._execution.coordinator_for(self._config, settings)
        self._charger = (
            inverter_device(settings, rest) if execution is None
            else inverter_device(settings, rest, execution=execution)
        )

    @device_action(Ownership.CHARGING)
    async def tick(
        self,
        target_w: float,
        *,
        soc: SocMeasurement | None = None,
        fallback_active: bool = False,
        fallback_confirmed: bool = False,
        now: datetime | None = None,
    ) -> ActionLine | None:
        """One guard pass; returns the action line if a resize was applied.

        ``soc`` is the daemon tick's checked measurement. A failed measurement
        caps the target at the live setpoint unless a fallback was confirmed.
        An unconfirmed fallback also caps increases; the supply-guard reading
        may still reduce the setpoint in either state.
        """
        s = self._settings
        try:
            supply = await self._rest.get_state(s.grid_power_entity)
            supply_w = float(supply.state)
            setpoint_w = await self._charger.read_charge_rate()
        except Exception as exc:  # noqa: BLE001 - never throttle on bad data
            log.warning("Supply guard: read failed (%s); skipping", exc)
            return None

        if select_limits(self._execution.limits_record, s).source == "unavailable":
            # #555: an unknown limit alone is no reason to cut; only fresh
            # evidence of over-import against the legacy setting is. Every
            # command narrows to 0 A here, through the same gate and read-back.
            if setpoint_w == 0 or not _fresh_over_legacy_limit(
                supply, supply_w, s, now or datetime.now(UTC),
            ):
                return None
            return await self._charger.set_charge_rate(0.0)

        if fallback_active:
            fallback_a = validated_fallback_current_a(s, self._execution)
            if not fallback_confirmed or fallback_a is None:
                # A persisted/requested/failed fallback is not evidence of the
                # resident current. Reductions remain valid; increases wait for
                # a fresh Solis read-back confirmation.
                target_w = min(target_w, setpoint_w)
            else:
                target_w = min(
                    target_w,
                    fallback_a * charge_voltage_v(s, self._execution),
                )
        elif soc is not None and not soc.ok:
            target_w = min(target_w, setpoint_w)

        # The effective connection import of the accepted limits (#542), as amps
        # at its validated supply voltage; unusable limits narrow it to zero.
        limits = active_limits(s, self._execution)
        import_w = 0.0 if isinstance(limits, str) else limits[1].connections[0].import_kw * 1000
        voltage_v = 1.0 if isinstance(limits, str) else limits[0].connections[0].supply_voltage_v
        wanted_w = throttled_rate_w(
            supply_w,
            setpoint_w,
            target_w,
            limit_a=import_w / voltage_v,
            supply_voltage_v=voltage_v,
        )
        if abs(wanted_w - setpoint_w) < _MIN_DELTA_W:
            return None
        return await self._charger.set_charge_rate(wanted_w)
