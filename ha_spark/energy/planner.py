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

from datetime import datetime, timedelta

from ha_spark.energy.models import (
    ChargeIntent,
    ChargePlan,
    ExportIntent,
    ExportSkip,
    PlannerConfig,
    PlannerInputs,
    Reservation,
)
from ha_spark.energy.tariff import TariffSchedule


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _slot_reservation(
    inputs: PlannerInputs,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    net: list[float],
) -> Reservation:
    """Build the tracer reservation for the slot-model horizon.

    The reservation ends at the first cheap slot after the current expensive
    run. If no later cheap slot is represented, the horizon end is the hard
    target. This makes the existing fixed schedule (cheap window first,
    expensive daytime afterward) reserve exactly its expensive-slot need.
    """
    fracs = schedule.cheap_fracs
    first_expensive = next(
        (i for i, fraction in enumerate(fracs) if fraction < 1.0 - 1e-9),
        None,
    )
    if first_expensive is None:
        target_slot = len(net)
    else:
        target_slot = next(
            (
                i
                for i in range(first_expensive + 1, len(fracs))
                if fracs[i] > 1e-9
            ),
            len(net),
        )

    expensive_need = sum(
        (1.0 - fraction) * energy
        for fraction, energy in zip(fracs[:target_slot], net[:target_slot], strict=True)
    )
    buffered_need = expensive_need * (1.0 + cfg.buffer_pct / 100.0)
    usable_capacity = max(0.0, cfg.capacity_kwh * (cfg.target_cap - cfg.min_soc) / 100.0)
    energy = min(buffered_need, usable_capacity)
    shortfall = max(0.0, buffered_need - energy)

    target_time = None
    target_label = f"slot {target_slot}"
    if inputs.horizon_start is not None:
        target_time = inputs.horizon_start + timedelta(minutes=30 * target_slot)
        target_label = (
            target_time.strftime("%H:%M") if target_slot < len(net) else "the horizon end"
        )

    if expensive_need <= 1e-9:
        reason = "No expensive forecast load needs to be carried to the next cheap slot."
    elif target_slot < len(net):
        reason = (
            f"Reserving {energy:.2f} kWh to cover forecast house load until the "
            f"{target_label} cheap slot."
        )
    else:
        reason = (
            f"Reserving {energy:.2f} kWh to cover forecast house load through the "
            "planning horizon because no later cheap slot appears in the horizon."
        )
    if shortfall > 1e-9:
        reason = (
            f"{reason[:-1]} The reservation is capped at usable battery capacity, "
            f"leaving a {shortfall:.2f} kWh forecast shortfall."
        )

    return Reservation(
        name="reach-next-cheap-slot",
        target_slot=target_slot,
        energy_kwh=energy,
        obligation_kind="reach-next-cheap-slot",
        reason=reason,
        target_time=target_time,
    )


def _overlaps(
    start: datetime, end: datetime, window: tuple[datetime, datetime]
) -> bool:
    return start < window[1] and window[0] < end


def _event_export_plan(
    inputs: PlannerInputs,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    net: list[float],
) -> tuple[ExportIntent | None, tuple[Reservation, ...], tuple[ExportSkip, ...]]:
    """Reserve and select one contiguous, fully fundable Axle export suffix.

    Work backwards from the event end.  That preserves the energy needed from
    the event end to the next cheap slot before admitting an export slot, and a
    suffix maps directly to an inverter's single timed-discharge window.
    """
    event = inputs.flexibility_event
    if event is None or event.direction != "export" or inputs.horizon_start is None:
        return None, (), ()

    event_slots: list[int] = []
    for i in range(len(net)):
        start = inputs.horizon_start + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        if event.start <= start and end <= event.end:
            event_slots.append(i)
    if not event_slots:
        return None, (), (
            ExportSkip(
                event.start,
                "Skipped paid event: it contains no complete slot in the planning horizon.",
            ),
        )

    # If any event slot overlaps the physical timed-charge window, export
    # outranks discretionary charging. Only energy already in the battery may
    # fund that event; the caller will emit a zero-charge intent below.
    n_charge_slots = int(schedule.window_hours * 2)
    charge_window_conflict = any(i < n_charge_slots for i in event_slots)
    usable_capacity = max(0.0, cfg.capacity_kwh * (cfg.target_cap - cfg.min_soc) / 100.0)
    if charge_window_conflict:
        usable_capacity = max(
            0.0, cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
        )

    # The named post-event reservation is computed first and never spent to
    # deliver an event.  Like the existing reservation, it carries the planner
    # safety buffer.
    after_event = event_slots[-1] + 1
    target_slot = next(
        (i for i in range(after_event, len(net)) if schedule.cheap_fracs[i] > 1e-9),
        len(net),
    )
    post_need = sum(
        (1.0 - schedule.cheap_fracs[i]) * net[i] for i in range(after_event, target_slot)
    )
    post_energy = min(
        post_need * (1.0 + cfg.buffer_pct / 100.0),
        usable_capacity,
    )
    target_time = inputs.horizon_start + timedelta(minutes=30 * target_slot)
    post = Reservation(
        name="reach-next-cheap-slot",
        target_slot=target_slot,
        energy_kwh=post_energy,
        obligation_kind="reach-next-cheap-slot",
        reason=(
            f"Reserving {post_energy:.2f} kWh after the Axle event to cover forecast house "
            f"load until the {target_time.strftime('%H:%M')} cheap slot."
            if target_slot < len(net)
            else (
                f"Reserving {post_energy:.2f} kWh after the Axle event through "
                "the planning horizon."
            )
        ),
        target_time=target_time,
    )

    # House energy from the end of overnight cheap coverage through the event
    # is also a concrete obligation. Without it, a plan can look funded on
    # paper yet spend the event reservation serving daytime load — including
    # an event slot skipped for lack of export funding — before export starts.
    pre_event_start = min(int(schedule.window_hours * 2), event_slots[0])
    pre_event_house_energy = sum(
        (1.0 - schedule.cheap_fracs[i]) * net[i]
        for i in range(pre_event_start, event_slots[0])
    )
    # Cheap grid never serves a slot while it is being exported: preserve its
    # whole forecast house load even when the event overlaps the charge window.
    event_house_energy = sum(net[i] for i in event_slots)
    house_through_event_energy = pre_event_house_energy + event_house_energy
    available = max(0.0, usable_capacity - post_energy - house_through_event_energy)
    supply_ceiling_kw = max(0.0, cfg.supply_max_current_a * cfg.supply_voltage_v / 1000.0)
    selected: list[tuple[int, float, float]] = []
    skips: list[ExportSkip] = []
    contiguous = True
    for i in reversed(event_slots):
        start = inputs.horizon_start + timedelta(minutes=30 * i)
        end = start + timedelta(minutes=30)
        solar_kw = (inputs.solar_slots or (0.0,) * len(net))[i] * cfg.solar_haircut_k * 2
        load_kw = inputs.load_slots[i] * 2 if inputs.load_slots is not None else 0.0
        unconstrained_export_kw = max(0.0, cfg.battery_discharge_ceiling_kw + solar_kw - load_kw)
        export_kw = min(
            cfg.dno_export_limit_kw,
            supply_ceiling_kw,
            unconstrained_export_kw,
        )
        held = any(_overlaps(start, end, window) for window in schedule.controlled_windows)
        # House load is already reserved across the entire event above, even
        # for a skipped slot. Selecting this slot only consumes export energy.
        energy = export_kw * 0.5
        if held:
            contiguous = False
            skips.append(
                ExportSkip(start, "Skipped paid slot: it overlaps an Octopus dispatch hold.")
            )
        elif unconstrained_export_kw > min(cfg.dno_export_limit_kw, supply_ceiling_kw) + 1e-9:
            contiguous = False
            skips.append(
                ExportSkip(
                    start,
                    "Skipped paid slot: the fixed discharge command would exceed "
                    "the DNO or supply limit.",
                )
            )
        elif export_kw <= 1e-9:
            contiguous = False
            skips.append(
                ExportSkip(
                    start,
                    "Skipped paid slot: forecast house load leaves no export capacity.",
                )
            )
        elif not contiguous:
            skips.append(
                ExportSkip(
                    start,
                    "Skipped paid slot: a later slot is unavailable, so one timed "
                    "window cannot cover it.",
                )
            )
        elif energy > available + 1e-9:
            contiguous = False
            skips.append(
                ExportSkip(
                    start,
                    "Skipped paid slot: funding it would consume reserved house "
                    "or post-event energy.",
                )
            )
        else:
            selected.append((i, export_kw, energy))
            available -= energy

    if not selected:
        return None, (), tuple(reversed(skips))

    selected.reverse()
    first, last = selected[0][0], selected[-1][0]
    selected_starts = tuple(
        inputs.horizon_start + timedelta(minutes=30 * i) for i, _, _ in selected
    )
    powers = tuple(power for _, power, _ in selected)
    event_energy = house_through_event_energy + sum(energy for _, _, energy in selected)
    window_end = inputs.horizon_start + timedelta(minutes=30 * (last + 1))
    event_reservation = Reservation(
        name="axle-export-event",
        target_slot=last + 1,
        energy_kwh=event_energy,
        obligation_kind="axle-export-event",
        reason=(
            f"Reserving {event_energy:.2f} kWh for forecast house load before and during "
            "paid Axle export from "
            f"{(inputs.horizon_start + timedelta(minutes=30 * first)).strftime('%H:%M')} "
            f"to {window_end.strftime('%H:%M')}, while protecting house load."
        ),
        target_time=window_end,
    )
    return (
        ExportIntent(
            event_identity=event.identity,
            window_start=inputs.horizon_start + timedelta(minutes=30 * first),
            window_end=window_end,
            planned_export_kw=min(powers),
            dno_export_limit_kw=cfg.dno_export_limit_kw,
            selected_slots=selected_starts,
            slot_export_kw=powers,
        ),
        (event_reservation, post),
        tuple(reversed(skips)),
    )


def compute_plan(
    inputs: PlannerInputs, cfg: PlannerConfig, schedule: TariffSchedule
) -> ChargePlan:
    """Compute the charge plan from live inputs, config, and a tariff schedule.

    ``schedule`` is the sole tariff contract: per-slot import prices/cheap
    fractions plus the controlled (held) windows and representative rates. It is
    required — a missing schedule was a silent revert to the ``fixed`` provider
    that let callers describe a different plan than the daemon applied (#91).
    For the legacy fixed-window two-rate behaviour, pass ``fixed_schedule``.
    """
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
    reservations: tuple[Reservation, ...] = ()
    export: ExportIntent | None = None
    export_skips: tuple[ExportSkip, ...] = ()
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
        export, event_reservations, export_skips = _event_export_plan(inputs, cfg, schedule, net)
        reservations = event_reservations or (_slot_reservation(inputs, cfg, schedule, net),)
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
    reservation_need = sum(reservation.energy_kwh for reservation in reservations)
    export_overlaps_charge = (
        export is not None
        and inputs.horizon_start is not None
        and any(
            int((start - inputs.horizon_start).total_seconds() // 1800)
            < int(schedule.window_hours * 2)
            for start in export.selected_slots
        )
    )
    if export_overlaps_charge:
        # The timed window cannot charge and discharge in the same slot.
        # Export wins; the existing battery energy was already used to vet it.
        required = 0.0
    elif cfg.strategy == "fill":
        # Fill to the cap regardless of need: optimal once export pays more
        # than off-peak; surplus carries over to later days (not costed here).
        required = headroom
    else:
        # The reservation only reaches the next cheap slot, which assumes that
        # slot can refill the battery. A short daytime dispatch cannot, so it
        # is a floor on the overnight buy, not the whole obligation — the
        # buffered horizon deficit still stands.
        obligation = max(reservation_need, buffered_deficit) if reservations else buffered_deficit
        required = _clamp(
            obligation - usable_at_window,
            0.0,
            headroom,
        )
    uncovered = max(0.0, buffered_deficit - usable_at_window - required)
    # The grid supplies required/efficiency AC kWh to store `required` kWh
    # (round-trip: AC->DC charging now, DC->AC discharge to the load later).
    efficiency = cfg.charge_efficiency if cfg.charge_efficiency > 0 else 1.0
    purchase = required / efficiency
    planned_cost = (cheap_net + purchase) * schedule.cheap_rate + uncovered * schedule.standard_rate

    # Paid-event export exists only in the planned path: without the battery
    # there is no dispatched export to sell.  Use the schedule's Axle overlay
    # when present, falling back to the validated event rate for pure-plan
    # callers that pass a fixed schedule in tests or tools.
    paid_export_revenue = 0.0
    if export is not None and inputs.horizon_start is not None:
        for start, power_kw in zip(export.selected_slots, export.slot_export_kw, strict=True):
            slot = int((start - inputs.horizon_start).total_seconds() // 1800)
            rate = (
                schedule.export_prices[slot]
                if slot < len(schedule.export_prices)
                else (inputs.flexibility_event.rate_gbp_kwh if inputs.flexibility_event else 0.0)
            )
            paid_export_revenue += power_kw * 0.5 * rate
        planned_cost -= paid_export_revenue

    # Incidental solar surplus is identical with or without overnight charge,
    # so it adjusts both projections. Paid-export revenue above changes only
    # the planned path.
    export_revenue: float | None = paid_export_revenue or None
    if schedule.export_rate > 0:
        solar_export_revenue = export_kwh * schedule.export_rate
        export_revenue = (export_revenue or 0.0) + solar_export_revenue
        baseline_cost -= solar_export_revenue
        planned_cost -= solar_export_revenue

    target_soc = inputs.soc_now
    if cfg.capacity_kwh > 0:
        target_soc = min(cfg.target_cap, inputs.soc_now + required / cfg.capacity_kwh * 100.0)

    holds = controlled
    intent = ChargeIntent(
        target_soc_pct=target_soc,
        soc=inputs.soc,
        window_start=cfg.window_start,
        window_end=cfg.window_end,
        holds=holds,
        export=export,
        hold_trusted=inputs.dispatches_trusted,
        export_trusted=inputs.flexibility_event_trusted,
    )

    return ChargePlan(
        soc=inputs.soc,
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
        reservations=reservations,
        export_skips=export_skips,
    )
