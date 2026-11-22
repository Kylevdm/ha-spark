"""Read pre-install HA hourly statistics into the add-on's energy ledger only."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from ha_spark.config import Settings
from ha_spark.energy.derived_base_load import ComponentSpec, derive_specs_from_settings
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.onboarding import (
    ENERGY_FACTORS,
    POWER_FACTORS,
    SUPPORTED_UNITS,
    statistic_unit,
)
from ha_spark.energy.plausibility import plausible, plausible_kw
from ha_spark.energy.readiness_ledger import slot_start
from ha_spark.energy.readiness_measurements import COMPONENTS, measurement_series
from ha_spark.energy.session_allowance import _opt_float
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
from ha_spark.logging import get_logger

log = get_logger(__name__)
BACKFILL_DAYS = 400


def _hourly_series(
    rows: Sequence[object], unit: str, invert: bool,
    start: datetime, cutoff: datetime, timezone: str, bound_kw: float,
) -> dict[datetime, ComponentFlow]:
    lower, upper = start.timestamp() * 1000, cutoff.timestamp() * 1000
    power = unit in POWER_FACTORS
    key = "mean" if power else "change"
    factor = (POWER_FACTORS if power else ENERGY_FACTORS)[unit] * (-1 if invert else 1)
    tz = load_timezone(timezone)
    hours: list[dict[str, object]] = []
    rejected: set[datetime] = set()
    for row in rows:
        if not isinstance(row, dict) or isinstance(row.get(key), bool):
            continue
        try:
            instant, value = _opt_float(row.get("start")), _opt_float(row.get(key))
        except OverflowError:
            continue
        # Validate before spreading: a negative hour must not blend into a valid slot.
        if not (instant is not None and lower <= instant < upper and instant % 3_600_000 == 0
                and value is not None and (-value if invert else value) >= 0):
            continue
        if not plausible(value * factor, 3600.0, bound_kw):
            # A rejected hour is never spread: every half-hour it touches
            # becomes a gap, not a partial share of the spike.
            hour = datetime.fromtimestamp(instant / 1000, UTC)
            ticks = {slot_start((hour + timedelta(minutes=5 * i)).astimezone(tz))
                     for i in range(12)}
            rejected.update(slot for slot in ticks if start <= slot < cutoff)
            continue
        hours.append({"start": instant, key: value})
    spread = {
        slot: ComponentFlow(kwh=measurement.kwh, provenance="hourly_backfill")
        for slot, measurement in measurement_series([], hours, unit, invert, timezone).items()
        if start <= slot < cutoff and measurement.provenance == "hourly_spread"
    }
    return spread | {slot: ComponentFlow(reason="rejected") for slot in rejected}


async def backfill_energy_ledger(settings: Settings) -> bool:
    """Try once; False means the daemon should retry later, not block a tick."""
    try:
        specs = derive_specs_from_settings(settings)
        if not specs["grid_import"].entity_id.strip():
            return False
        ledger = EnergyLedger(settings.db_path)
        cutoff = await ledger.first_recorder_slot()
        if cutoff is None:
            return False
        bound_kw = plausible_kw(settings)
        specs["v2l"] = ComponentSpec(settings.v2l_power_entity)
        start = cutoff - timedelta(days=BACKFILL_DAYS)
        url, token, timeout = settings.ha_websocket_url, settings.auth_token, settings.ha_timeout
        metas = await list_statistic_ids(url, token, timeout=timeout)
        units = {
            meta["statistic_id"]: statistic_unit(meta)
            for meta in metas
            if isinstance(meta, dict) and isinstance(meta.get("statistic_id"), str)
        }
        series: dict[str, dict[datetime, ComponentFlow]] = {}
        missing: dict[str, ComponentFlow] = {}
        for name in COMPONENTS:
            spec = specs.get(name, ComponentSpec(""))
            entity = spec.entity_id.strip()
            unit = units.get(entity)
            reason = "not_configured" if not entity else "hourly_statistic_missing"
            series[name] = {}
            if entity and unit is not None:
                if unit not in SUPPORTED_UNITS:
                    reason = "unsupported_unit"
                else:
                    # Bounded at the cutoff: the read stays ~BACKFILL_DAYS on old installs.
                    rows = await statistics_during_period(
                        url, token, entity, start, period="hour", timeout=timeout,
                        end_time=cutoff,
                    )
                    series[name] = await asyncio.to_thread(
                        _hourly_series, rows, unit, spec.invert, start, cutoff,
                        settings.timezone, bound_kw,
                    )
            missing[name] = ComponentFlow(reason=reason)

        flows = {
            slot: {name: series[name].get(slot, missing[name]) for name in COMPONENTS}
            for slot in series["grid_import"]
        }
        if any(flow.reason == "rejected" for slots in series.values() for flow in slots.values()):
            log.warning("Energy ledger rejected an implausible interval")
        await ledger.record_hourly_backfill(flows)
        return True
    except Exception:
        log.warning("Energy ledger backfill failed")
        return False
