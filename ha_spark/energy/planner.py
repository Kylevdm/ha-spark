"""The deterministic charge planner — pure functions, no I/O.

v1 model (daily energy balance, used when no per-slot forecast is available):

    effective_solar = solar_tomorrow * haircut_k
    cheap_covered   = home-load energy during daytime dispatch slots (cheap grid)
    deficit         = max(0, home_load - effective_solar - cheap_covered)
    usable_now      = capacity * (soc_now - min_soc) / 100
    usable_at_window= usable_now - pre_window_drain   (load before the window opens)
    buffered        = deficit * (1 + buffer_pct / 100)
    required        = clamp(buffered - usable_at_window, 0, headroom_to_cap)
    purchase        = required / charge_efficiency   (AC kWh bought)

``compute_plan`` turns ``required``/``purchase`` into a ``target_soc`` and emits
a ``ChargeIntent``; per-inverter charge mechanics (e.g. amps sizing for Solis)
are the adapter's job, not the planner's.

With ``strategy="fill"`` the sizing instead charges to the target cap every
night (``required = headroom``) — optimal once the export rate exceeds
off-peak; the carried-over surplus is an asset the cost projection does not
model.

v2 model (per-slot horizon, when ``inputs.load_slots`` is set): the horizon is 48
half-hour slots starting at the charge-window start tonight. Slots inside the
fixed window, or overlapping an Octopus dispatch, are "cheap"; the battery only
needs to cover the *expensive* slots' net load (load - solar), so

    expensive_need  = sum_slots (1 - cheap_frac) * max(0, load - solar)

replaces ``deficit``, then the same buffer and clamps apply. Both models also
project a two-rate cost (off-peak/peak) with and without the battery.

Daytime dispatch slots are expressed as ``holds`` on the ``ChargeIntent``, so
the battery holds (doesn't discharge) while cheap grid covers the house.
"""

from __future__ import annotations

from ha_spark.energy.models import (
    ChargeIntent,
    ChargePlan,
    PlannerConfig,
    PlannerInputs,
)
from ha_spark.energy.tariff import TariffSchedule, fixed_schedule


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_plan(
    inputs: PlannerInputs, cfg: PlannerConfig, schedule: TariffSchedule | None = None
) -> ChargePlan:
    """Compute the charge plan from live inputs, config, and a tariff schedule.

    ``schedule`` is the sole tariff contract: per-slot import prices/cheap
    fractions plus the controlled (held) windows and representative rates.
    When ``None`` it defaults to the ``fixed`` provider derived from ``cfg``,
    which reproduces the legacy fixed-window two-rate behaviour byte-for-byte.
    """
    if schedule is None:
        schedule = fixed_schedule(inputs, cfg)
    effective_solar = inputs.solar_tomorrow_kwh * cfg.solar_haircut_k

    # Daytime dispatch slots (the schedule's controlled windows) run the house
    # off cheap grid so that load needn't come from the battery; in the daily
    # model approximate it as avg home power over the window duration (the slot
    # model accounts for it per-slot instead).
    avg_home_power_kw = inputs.predicted_home_load_kwh / 24.0
    controlled = schedule.controlled_windows

    usable_now = cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
    headroom = max(0.0, cfg.capacity_kwh * (cfg.target_cap - inputs.soc_now) / 100.0)

    expensive_load_kwh: float | None = None
    if inputs.load_slots is not None:
        # --- v2 per-slot horizon: cost against the schedule's per-slot prices ---
        model = "slots"
        n = len(inputs.load_slots)
        solar_slots = inputs.solar_slots or (0.0,) * n
        net = [
            max(0.0, load - solar * cfg.solar_haircut_k)
            for load, solar in zip(inputs.load_slots, solar_slots, strict=False)
        ]
        fracs = schedule.cheap_fracs
        prices = schedule.prices
        expensive_need = sum((1.0 - f) * e for f, e in zip(fracs, net, strict=True))
        expensive_load_kwh = expensive_need
        deficit = expensive_need
        # Dispatch-covered load outside the fixed window (for the report).
        n_window = int(schedule.window_hours * 2)
        cheap_covered = sum(
            f * e for i, (f, e) in enumerate(zip(fracs, net, strict=True)) if i >= n_window
        )
        baseline_cost = sum(e * p for p, e in zip(prices, net, strict=True))
        cheap_net = sum(f * e for f, e in zip(fracs, net, strict=True))
        export_kwh = sum(
            max(0.0, solar * cfg.solar_haircut_k - load)
            for load, solar in zip(inputs.load_slots, solar_slots, strict=False)
        )
    else:
        # --- v1 daily balance ---
        model = "daily"
        cheap_covered = (
            sum(max(0.0, (b - a).total_seconds() / 3600.0) for a, b in controlled)
            * avg_home_power_kw
        )
        deficit = max(0.0, inputs.predicted_home_load_kwh - effective_solar - cheap_covered)
        # Daily-total cost approximation: window-time load is off-peak even
        # without a battery; dispatch slots cover `cheap_covered` off-peak.
        net_total = max(0.0, inputs.predicted_home_load_kwh - effective_solar)
        window_load = inputs.predicted_home_load_kwh * schedule.window_hours / 24.0
        cheap_net = min(net_total, cheap_covered + window_load)
        baseline_cost = (
            cheap_net * schedule.cheap_rate + (net_total - cheap_net) * schedule.standard_rate
        )
        export_kwh = max(0.0, effective_solar - inputs.predicted_home_load_kwh)

    buffered_deficit = deficit * (1.0 + cfg.buffer_pct / 100.0)
    # The horizon starts at the window, so load between now and then drains
    # the battery invisibly — size against the usable energy at window start.
    usable_at_window = usable_now - inputs.pre_window_drain_kwh
    if cfg.strategy == "fill":
        # Fill to the cap regardless of need: optimal once export pays more
        # than off-peak; surplus carries over to later days (not costed here).
        required = headroom
    else:
        required = _clamp(buffered_deficit - usable_at_window, 0.0, headroom)
    uncovered = max(0.0, buffered_deficit - usable_at_window - required)
    # The grid supplies required/efficiency AC kWh to store `required` kWh
    # (round-trip: AC->DC charging now, DC->AC discharge to the load later).
    efficiency = cfg.charge_efficiency if cfg.charge_efficiency > 0 else 1.0
    purchase = required / efficiency
    planned_cost = (cheap_net + purchase) * schedule.cheap_rate + uncovered * schedule.standard_rate

    # Export revenue is identical with or without the overnight charge, so it
    # adjusts both projections (reporting honesty) without changing decisions.
    export_revenue: float | None = None
    if schedule.export_rate > 0:
        export_revenue = export_kwh * schedule.export_rate
        baseline_cost -= export_revenue
        planned_cost -= export_revenue

    target_soc = inputs.soc_now
    if cfg.capacity_kwh > 0:
        target_soc = min(cfg.target_cap, inputs.soc_now + required / cfg.capacity_kwh * 100.0)

    holds = controlled
    intent = ChargeIntent(
        target_soc_pct=target_soc,
        soc_now=inputs.soc_now,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
        holds=holds,
        soc_valid=inputs.soc_valid,
    )

    return ChargePlan(
        soc_now=inputs.soc_now,
        soc_valid=inputs.soc_valid,
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
        window_hours=cfg.window_hours,
        ev_charging=inputs.ev_charging,
        ha_template_needed=inputs.ha_template_needed,
        charge_intent=intent,
        model=model,
        expensive_load_kwh=expensive_load_kwh,
        slot_prices=schedule.prices or None,
        baseline_cost=baseline_cost,
        planned_cost=planned_cost,
        charge_efficiency=efficiency,
        export_revenue=export_revenue,
        strategy=cfg.strategy,
        pre_window_drain_kwh=inputs.pre_window_drain_kwh,
        # Octopus reports planned charge_in_kwh as negative (energy into the
        # car); report the magnitude.
        dispatch_ev_kwh=(
            sum(abs(d.charge_in_kwh) for d in inputs.dispatches) if inputs.dispatches else None
        ),
    )
