"""The deterministic charge planner — pure functions, no I/O.

v1 model (daily energy balance, used when no per-slot forecast is available):

    effective_solar = solar_tomorrow * haircut_k
    cheap_covered   = home-load energy during daytime dispatch slots (cheap grid)
    deficit         = max(0, home_load - effective_solar - cheap_covered)
    usable_now      = capacity * (soc_now - min_soc) / 100
    buffered        = deficit * (1 + buffer_pct / 100)
    required        = clamp(buffered - usable_now, 0, headroom_to_cap)
    current_A       = clamp(required / (window_h * voltage/1000), 0, max_A)

v2 model (per-slot horizon, when ``inputs.load_slots`` is set): the horizon is 48
half-hour slots starting at the charge-window start tonight. Slots inside the
fixed window, or overlapping an Octopus dispatch, are "cheap"; the battery only
needs to cover the *expensive* slots' net load (load - solar), so

    expensive_need  = sum_slots (1 - cheap_frac) * max(0, load - solar)

replaces ``deficit``, then the same buffer and clamps apply. Both models also
project a two-rate cost (off-peak/peak) with and without the battery.

Daytime dispatch slots each emit a ``stop_discharge`` action so the battery
holds (doesn't feed the EV) while cheap grid covers the house.
"""

from __future__ import annotations

from datetime import datetime, time

from ha_spark.energy.models import (
    ChargeAction,
    ChargePlan,
    DispatchSlot,
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


def _hours_since(origin: datetime, dt: datetime) -> float:
    """Hours from ``origin`` to ``dt``, coercing a naive ``dt`` into origin's tz."""
    if dt.tzinfo is None and origin.tzinfo is not None:
        dt = dt.replace(tzinfo=origin.tzinfo)
    elif dt.tzinfo is not None and origin.tzinfo is None:
        origin = origin.replace(tzinfo=dt.tzinfo)
    return (dt - origin).total_seconds() / 3600.0


def _cheap_fractions(
    n_slots: int,
    window_hours: float,
    horizon_start: datetime | None,
    dispatches: tuple[DispatchSlot, ...],
) -> list[float]:
    """Per-slot fraction billed off-peak: the fixed window plus dispatch overlap."""
    n_window = int(window_hours * 2)
    fracs = [1.0 if i < n_window else 0.0 for i in range(n_slots)]
    if horizon_start is None:
        return fracs
    for d in dispatches:
        a = _hours_since(horizon_start, d.start)
        b = _hours_since(horizon_start, d.end)
        for i in range(n_slots):
            slot_start, slot_end = i * 0.5, (i + 1) * 0.5
            overlap = max(0.0, min(b, slot_end) - max(a, slot_start)) / 0.5
            if overlap > 0:
                fracs[i] = min(1.0, fracs[i] + overlap)
    return fracs


def compute_plan(inputs: PlannerInputs, cfg: PlannerConfig) -> ChargePlan:
    """Compute the charge plan from live inputs and fixed config."""
    effective_solar = inputs.solar_tomorrow_kwh * cfg.solar_haircut_k

    # Daytime dispatch slots run the house off cheap grid, so that load needn't come
    # from the battery; in the daily model approximate it as avg home power over the
    # slot duration (the slot model accounts for it per-slot instead).
    avg_home_power_kw = inputs.predicted_home_load_kwh / 24.0
    daytime = tuple(
        d
        for d in inputs.dispatches
        if not _in_overnight_window(d.start.time(), cfg.window_start, cfg.window_end)
    )

    usable_now = cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
    headroom = max(0.0, cfg.capacity_kwh * (cfg.target_cap - inputs.soc_now) / 100.0)

    expensive_load_kwh: float | None = None
    if inputs.load_slots is not None:
        # --- v2 per-slot horizon ---
        model = "slots"
        n = len(inputs.load_slots)
        solar_slots = inputs.solar_slots or (0.0,) * n
        net = [
            max(0.0, load - solar * cfg.solar_haircut_k)
            for load, solar in zip(inputs.load_slots, solar_slots, strict=False)
        ]
        fracs = _cheap_fractions(n, cfg.window_hours, inputs.horizon_start, inputs.dispatches)
        expensive_need = sum((1.0 - f) * e for f, e in zip(fracs, net, strict=True))
        expensive_load_kwh = expensive_need
        deficit = expensive_need
        # Dispatch-covered load outside the fixed window (for the report).
        n_window = int(cfg.window_hours * 2)
        cheap_covered = sum(
            f * e for i, (f, e) in enumerate(zip(fracs, net, strict=True)) if i >= n_window
        )
        baseline_cost = sum(
            e * (f * cfg.rate_offpeak + (1.0 - f) * cfg.rate_peak)
            for f, e in zip(fracs, net, strict=True)
        )
        cheap_net = sum(f * e for f, e in zip(fracs, net, strict=True))
    else:
        # --- v1 daily balance ---
        model = "daily"
        cheap_covered = sum(d.hours for d in daytime) * avg_home_power_kw
        deficit = max(0.0, inputs.predicted_home_load_kwh - effective_solar - cheap_covered)
        # Daily-total cost approximation: window-time load is off-peak even
        # without a battery; dispatch slots cover `cheap_covered` off-peak.
        net_total = max(0.0, inputs.predicted_home_load_kwh - effective_solar)
        window_load = inputs.predicted_home_load_kwh * cfg.window_hours / 24.0
        cheap_net = min(net_total, cheap_covered + window_load)
        baseline_cost = cheap_net * cfg.rate_offpeak + (net_total - cheap_net) * cfg.rate_peak

    buffered_deficit = deficit * (1.0 + cfg.buffer_pct / 100.0)
    required = _clamp(buffered_deficit - usable_now, 0.0, headroom)
    uncovered = max(0.0, buffered_deficit - usable_now - required)
    planned_cost = (cheap_net + required) * cfg.rate_offpeak + uncovered * cfg.rate_peak

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
        deficit_kwh=deficit,
        buffer_pct=cfg.buffer_pct,
        required_kwh=required,
        target_soc=target_soc,
        overnight_current_a=current,
        window_hours=cfg.window_hours,
        ev_charging=inputs.ev_charging,
        ha_template_needed=inputs.ha_template_needed,
        actions=tuple(actions),
        model=model,
        expensive_load_kwh=expensive_load_kwh,
        baseline_cost=baseline_cost,
        planned_cost=planned_cost,
    )
