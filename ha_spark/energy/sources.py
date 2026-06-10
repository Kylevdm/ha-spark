"""Gather live planner inputs from Home Assistant (REST reads)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone, predict_home_load
from ha_spark.energy.models import SLOTS_PER_DAY, DispatchSlot, PlannerConfig, PlannerInputs
from ha_spark.energy.solar import distribute_solar
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

# myenergi zappi states that mean the EV is actively drawing power.
_EV_ACTIVE = {"charging", "delivering", "boosting", "diverting"}


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def _parse_dispatches(raw: Any) -> tuple[DispatchSlot, ...]:
    slots: list[DispatchSlot] = []
    for d in raw or []:
        if not isinstance(d, dict):
            continue
        try:
            start = datetime.fromisoformat(str(d["start"]))
            end = datetime.fromisoformat(str(d["end"]))
        except (KeyError, ValueError, TypeError):
            continue
        slots.append(
            DispatchSlot(
                start,
                end,
                _to_float(d.get("charge_in_kwh"), 0.0),
                str(d.get("source", "")),
            )
        )
    return tuple(slots)


def _parse_detailed_forecast(
    raw: Any, percentile: int = 50
) -> list[tuple[datetime, float]] | None:
    """Tolerantly parse Solcast's ``detailedForecast`` attribute (shape varies).

    ``percentile`` selects ``pv_estimate10``/``pv_estimate90`` when present,
    falling back to the median ``pv_estimate``.
    """
    if not isinstance(raw, list):
        return None
    key = "pv_estimate" if percentile == 50 else f"pv_estimate{percentile}"
    entries: list[tuple[datetime, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = datetime.fromisoformat(str(item["period_start"]))
            value = item.get(key)
            if value is None:
                value = item["pv_estimate"]
            entries.append((start, float(value)))
        except (KeyError, ValueError, TypeError):
            continue
    return entries or None


def _slot_horizon(
    day_slots: tuple[float, ...], window_start: time, window_end: time, tz: ZoneInfo
) -> tuple[tuple[float, ...], datetime]:
    """Rotate slot-of-day values so index 0 is the charge-window start tonight.

    The horizon spans two calendar days but uses one day's profile values
    throughout — adjacent days share a day-type often enough that the error in
    tonight's pre-midnight slots is negligible.
    """
    start_idx = window_start.hour * 2 + window_start.minute // 30
    rotated = tuple(day_slots[(start_idx + i) % SLOTS_PER_DAY] for i in range(SLOTS_PER_DAY))
    tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
    origin_date = tomorrow - timedelta(days=1) if window_start >= window_end else tomorrow
    horizon_start = datetime.combine(origin_date, window_start, tzinfo=tz)
    return rotated, horizon_start


def build_config(settings: Settings, voltage_v: float) -> PlannerConfig:
    return PlannerConfig(
        capacity_kwh=settings.battery_capacity_kwh,
        voltage_v=voltage_v,
        min_soc=settings.min_soc,
        target_cap=settings.target_soc_cap,
        max_current_a=settings.max_charge_current_a,
        solar_haircut_k=settings.solar_haircut_k,
        window_start=parse_time(settings.charge_window_start),
        window_end=parse_time(settings.charge_window_end),
        rate_offpeak=settings.rate_offpeak_gbp_kwh,
        rate_peak=settings.rate_peak_gbp_kwh,
        rate_export=settings.rate_export_gbp_kwh,
        buffer_pct=settings.charge_buffer_pct,
        charge_efficiency=settings.charge_efficiency,
    )


async def gather_inputs(
    settings: Settings, rest: HomeAssistantRest
) -> tuple[PlannerInputs, PlannerConfig, str]:
    """Read live HA state and build (inputs, config, load-forecast source)."""

    async def state(entity_id: str) -> EntityState | None:
        try:
            return await rest.get_state(entity_id)
        except Exception as exc:  # noqa: BLE001 - a missing entity must not crash the plan
            log.warning("Could not read %s (%s)", entity_id, exc)
            return None

    soc = await state(settings.soc_entity)
    voltage = await state(settings.battery_voltage_entity)
    solar = await state(settings.solar_tomorrow_entity)
    dispatch = await state(settings.dispatch_entity)
    ev_status = await state(settings.ev_status_entity)
    ha_needed = await state(settings.ha_template_charge_needed_entity)

    voltage_v = _to_float(voltage.state if voltage else None, settings.battery_voltage_v)
    dispatches = _parse_dispatches(
        dispatch.attributes.get("planned_dispatches") if dispatch else None
    )
    ev_charging = bool(ev_status and str(ev_status.state).lower() in _EV_ACTIVE)

    forecast = await predict_home_load(settings)
    solar_kwh = _to_float(solar.state if solar else None, 0.0)
    # The sensor state is Solcast's median day total; estimate10/estimate90
    # attributes carry the conservative/optimistic totals.
    if settings.solar_percentile != 50 and solar is not None:
        percentile_total = _opt_float(
            solar.attributes.get(f"estimate{settings.solar_percentile}")
        )
        if percentile_total is not None:
            solar_kwh = percentile_total

    load_slots: tuple[float, ...] | None = None
    solar_slots: tuple[float, ...] | None = None
    horizon_start: datetime | None = None
    if forecast.slots is not None:
        tz = load_timezone(settings.timezone)
        window_start = parse_time(settings.charge_window_start)
        window_end = parse_time(settings.charge_window_end)
        load_slots, horizon_start = _slot_horizon(forecast.slots, window_start, window_end, tz)
        tomorrow = (datetime.now(tz) + timedelta(days=1)).date()
        detailed = _parse_detailed_forecast(
            solar.attributes.get("detailedForecast") if solar else None,
            settings.solar_percentile,
        )
        solar_day = distribute_solar(solar_kwh, detailed, tz, tomorrow)
        solar_slots, _ = _slot_horizon(solar_day, window_start, window_end, tz)

    inputs = PlannerInputs(
        soc_now=_to_float(soc.state if soc else None, 0.0),
        soc_valid=_opt_float(soc.state if soc else None) is not None,
        solar_tomorrow_kwh=solar_kwh,
        predicted_home_load_kwh=forecast.total_kwh,
        dispatches=dispatches,
        ev_charging=ev_charging,
        ha_template_needed=_opt_float(ha_needed.state) if ha_needed else None,
        load_slots=load_slots,
        solar_slots=solar_slots,
        horizon_start=horizon_start,
    )
    return inputs, build_config(settings, voltage_v), forecast.source
