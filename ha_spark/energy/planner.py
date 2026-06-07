"""The deterministic charge planner — pure functions, no I/O.

Model (v1, daily energy balance):

    effective_solar = solar_tomorrow * haircut_k
    cheap_covered   = home-load energy during daytime dispatch slots (cheap grid)
    deficit         = max(0, home_load - effective_solar - cheap_covered)
    usable_now      = capacity * (soc_now - min_soc) / 100
    required        = clamp(deficit - usable_now, 0, headroom_to_cap)
    current_A       = clamp(required / (window_h * voltage/1000), 0, max_A)

Daytime dispatch slots also each emit a ``stop_discharge`` action so the battery
holds (doesn't feed the EV) while cheap grid covers the house.
"""

from __future__ import annotations

from datetime import time

from ha_spark.energy.models import (
    ChargeAction,
    ChargePlan,
    PlannerConfig,
    PlannerInputs,
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _in_overnight_window(t: time, start: time, end: time) -> bool:
    """True if clock time ``t`` falls in the charge window (which may wrap midnight)."""
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end


def compute_plan(inputs: PlannerInputs, cfg: PlannerConfig) -> ChargePlan:
    """Compute the charge plan from live inputs and fixed config."""
    effective_solar = inputs.solar_tomorrow_kwh * cfg.solar_haircut_k

    # Daytime dispatch slots run the house off cheap grid, so that load needn't come
    # from the battery; approximate it as avg home power over the slot duration.
    avg_home_power_kw = inputs.predicted_home_load_kwh / 24.0
    daytime = tuple(
        d
        for d in inputs.dispatches
        if not _in_overnight_window(d.start.time(), cfg.window_start, cfg.window_end)
    )
    cheap_covered = sum(d.hours for d in daytime) * avg_home_power_kw

    deficit = max(0.0, inputs.predicted_home_load_kwh - effective_solar - cheap_covered)
    usable_now = cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
    headroom = max(0.0, cfg.capacity_kwh * (cfg.target_cap - inputs.soc_now) / 100.0)
    required = _clamp(deficit - usable_now, 0.0, headroom)

    target_soc = inputs.soc_now
    if cfg.capacity_kwh > 0:
        target_soc = min(cfg.target_cap, inputs.soc_now + required / cfg.capacity_kwh * 100.0)

    # required kWh over the fixed window -> charge current (A).
    kwh_per_amp = cfg.window_hours * cfg.voltage_v / 1000.0
    current = _clamp(required / kwh_per_amp, 0.0, cfg.max_current_a) if kwh_per_amp > 0 else 0.0

    actions: list[ChargeAction] = [
        ChargeAction(
            kind="set_charge_current",
            description=(
                f"set timed charge current to {current:.0f} A "
                f"for the {cfg.window_hours:.1f} h window"
            ),
            current_a=round(current),
        )
    ]
    for d in daytime:
        actions.append(
            ChargeAction(
                kind="stop_discharge",
                description=(
                    "turn inverter off (stop discharge) during dispatch "
                    f"{d.start:%H:%M}-{d.end:%H:%M}"
                ),
                slot_start=d.start,
                slot_end=d.end,
            )
        )

    return ChargePlan(
        soc_now=inputs.soc_now,
        capacity_kwh=cfg.capacity_kwh,
        solar_kwh=inputs.solar_tomorrow_kwh,
        effective_solar_kwh=effective_solar,
        load_kwh=inputs.predicted_home_load_kwh,
        cheap_covered_kwh=cheap_covered,
        usable_now_kwh=usable_now,
        required_kwh=required,
        target_soc=target_soc,
        overnight_current_a=current,
        window_hours=cfg.window_hours,
        ev_charging=inputs.ev_charging,
        ha_template_needed=inputs.ha_template_needed,
        actions=tuple(actions),
    )
