"""Gather live planner inputs from Home Assistant (REST reads)."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from ha_spark.config import Settings
from ha_spark.energy.forecast import predict_home_load_kwh
from ha_spark.energy.models import DispatchSlot, PlannerConfig, PlannerInputs
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


def _parse_time(hhmm: str) -> time:
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


def build_config(settings: Settings, voltage_v: float) -> PlannerConfig:
    return PlannerConfig(
        capacity_kwh=settings.battery_capacity_kwh,
        voltage_v=voltage_v,
        min_soc=settings.min_soc,
        target_cap=settings.target_soc_cap,
        max_current_a=settings.max_charge_current_a,
        solar_haircut_k=settings.solar_haircut_k,
        window_start=_parse_time(settings.charge_window_start),
        window_end=_parse_time(settings.charge_window_end),
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

    load_kwh, load_source = await predict_home_load_kwh(settings)

    inputs = PlannerInputs(
        soc_now=_to_float(soc.state if soc else None, 0.0),
        solar_tomorrow_kwh=_to_float(solar.state if solar else None, 0.0),
        predicted_home_load_kwh=load_kwh,
        dispatches=dispatches,
        ev_charging=ev_charging,
        ha_template_needed=_opt_float(ha_needed.state) if ha_needed else None,
    )
    return inputs, build_config(settings, voltage_v), load_source
