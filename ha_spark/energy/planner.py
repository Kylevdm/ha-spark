"""The deterministic charge planner — pure functions, no I/O.

v1 model (daily energy balance, used when no per-slot forecast is available):

    effective_solar = solar_tomorrow * haircut_k
    cheap_covered   = home-load energy during daytime dispatch slots (cheap grid)
    deficit         = max(0, home_load - effective_solar - cheap_covered)
    usable_now      = capacity * (soc_now - min_soc) / 100
    usable_at_window= usable_now - pre_window_drain   (load before the window opens)
    buffered        = deficit * (1 + buffer_pct / 100)
    required        = clamp(buffered - usable_at_window, 0,
                            min(headroom_to_cap, window_charge_limit))
    purchase        = required / charge_efficiency   (AC kWh bought)

``window_charge_limit`` is what the charge window can add at ``max_current_a``
(only the rest of the window once it has started).

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

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import NamedTuple

from ha_spark.energy.models import (
    ChargeIntent,
    ChargePlan,
    ExportIntent,
    ExportSkip,
    FlexibilityEvent,
    PlannerConfig,
    PlannerInputs,
    Reservation,
)
from ha_spark.energy.tariff import TariffSchedule, _controlled_windows


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


def _window_charge_limit(inputs: PlannerInputs, cfg: PlannerConfig) -> float:
    """Battery kWh the charge window can add at ``max_current_a`` (#208).

    Once the window has started only the rest of it counts.
    """
    hours = cfg.window_hours
    if inputs.now is not None and inputs.horizon_start is not None:
        elapsed = (inputs.now - inputs.horizon_start).total_seconds() / 3600.0
        if 0.0 <= elapsed < hours:
            hours -= elapsed
    return max(0.0, cfg.max_current_a * cfg.voltage_v * hours / 1000.0)


def _overlaps(
    start: datetime, end: datetime, window: tuple[datetime, datetime]
) -> bool:
    return start < window[1] and window[0] < end


class _EventSlot(NamedTuple):
    """One complete paid-event slot offered for export."""

    start: datetime
    solar_kw: float
    load_kw: float
    # Hours of the slot left to export: under 0.5 for the slot in progress.
    hours: float = 0.5
    # A skip reason that rules the slot out before any other check.
    refused: str | None = None


class _Reserve(NamedTuple):
    """The post-event reservation an event may spend when buying it back pays (#207)."""

    # What the battery holds of the reservation, above ``min_soc``.
    energy: float
    event_rate: float
    efficiency: float
    # The worst import price over the slots a shortfall of this many kWh lands in.
    price_for: Callable[[float], float]

    def buy_back(self, shortfall: float) -> float:
        """GBP/kWh to buy ``shortfall`` back, after round-trip losses."""
        return self.price_for(shortfall) / self.efficiency

    def funds(self, shortfall: float) -> bool:
        return shortfall <= self.energy + 1e-9 and self.event_rate > self.buy_back(shortfall)

    def funding(self, spent: float) -> str:
        return (
            f"Funded {spent:.2f} kWh from the post-event reserve: "
            f"£{self.event_rate:.2f} event vs £{self.buy_back(spent):.2f} buy-back."
        )


_EXPORT_FUNDING_SKIP_REASON = (
    "Skipped paid slot: funding it would consume reserved house or post-event energy."
)
_EXPORT_LATER_UNAVAILABLE_SKIP_REASON = (
    "Skipped paid slot: a later slot is unavailable, so one timed window cannot cover it."
)


def _slot_export_kw(slot: _EventSlot, cfg: PlannerConfig) -> float:
    supply_ceiling_kw = max(0.0, cfg.supply_max_current_a * cfg.supply_voltage_v / 1000.0)
    unconstrained_export_kw = max(
        0.0, cfg.battery_discharge_ceiling_kw + slot.solar_kw - slot.load_kw
    )
    return min(cfg.dno_export_limit_kw, supply_ceiling_kw, unconstrained_export_kw)


def _export_soc_needed(
    slots: list[_EventSlot],
    selected: list[tuple[_EventSlot, float]],
    skips: tuple[ExportSkip, ...],
    house_energy_by_start: dict[datetime, float],
    buffered_post_energy: float,
    cfg: PlannerConfig,
) -> tuple[float | None, datetime | None]:
    """Return the SoC needed at the counted suffix start, when funding was skipped."""
    if not any(skip.reason == _EXPORT_FUNDING_SKIP_REASON for skip in skips):
        return None, None

    selected_power = {slot.start: power for slot, power in selected}
    skip_reason_by_start = {skip.start: skip.reason for skip in skips}
    counted_reverse: list[_EventSlot] = []
    funding_in_suffix = False
    for slot in reversed(slots):
        if slot.start in selected_power:
            counted_reverse.append(slot)
            continue
        reason = skip_reason_by_start.get(slot.start)
        if reason == _EXPORT_FUNDING_SKIP_REASON:
            funding_in_suffix = True
            counted_reverse.append(slot)
        elif reason == _EXPORT_LATER_UNAVAILABLE_SKIP_REASON and funding_in_suffix:
            counted_reverse.append(slot)
        else:
            # A DNO, supply, hold, daylight, or capacity refusal ends the
            # suffix. Slots before it cannot be added by more battery energy.
            break
    if not funding_in_suffix or not counted_reverse or cfg.capacity_kwh <= 0:
        return None, None
    counted_slots = list(reversed(counted_reverse))
    counted_export_energy = sum(
        (selected_power[slot.start] if slot.start in selected_power else _slot_export_kw(slot, cfg))
        * slot.hours
        for slot in counted_slots
    )
    counted_house_energy = sum(house_energy_by_start[slot.start] for slot in counted_slots)
    needed_pct = cfg.min_soc + 100.0 * (
        counted_house_energy + counted_export_energy + buffered_post_energy
    ) / cfg.capacity_kwh
    return needed_pct, counted_slots[0].start


def _select_export_suffix(
    slots: list[_EventSlot],
    available: float,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    reserve: _Reserve | None = None,
) -> tuple[list[tuple[_EventSlot, float]], tuple[ExportSkip, ...], float]:
    """Walk an event's complete slots backwards and keep one fundable suffix.

    ``slots`` is in time order, and ``available`` is the battery energy left for
    export once house load and the post-event reservation are protected. A slot
    ``available`` can't fund may spend the ``reserve`` when buying it back costs
    less than the event pays. Returns the selected slots with their export
    power, then the skips, both in time order, then the reserve spent.
    """
    selected: list[tuple[_EventSlot, float]] = []
    skips: list[ExportSkip] = []
    contiguous = True
    spent = 0.0
    supply_ceiling_kw = max(0.0, cfg.supply_max_current_a * cfg.supply_voltage_v / 1000.0)
    for slot in reversed(slots):
        start = slot.start
        end = start + timedelta(minutes=30)
        unconstrained_export_kw = max(
            0.0, cfg.battery_discharge_ceiling_kw + slot.solar_kw - slot.load_kw
        )
        export_kw = _slot_export_kw(slot, cfg)
        held = any(_overlaps(start, end, window) for window in schedule.controlled_windows)
        # House load is already reserved across the entire event, even for a
        # skipped slot. Selecting this slot only consumes export energy.
        energy = export_kw * slot.hours
        if slot.refused is not None:
            contiguous = False
            skips.append(ExportSkip(start, slot.refused))
        elif held:
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
                    _EXPORT_LATER_UNAVAILABLE_SKIP_REASON,
                )
            )
        elif energy > available + 1e-9 and not (
            reserve is not None and reserve.funds(spent + energy - available)
        ):
            contiguous = False
            skips.append(
                ExportSkip(start, _EXPORT_FUNDING_SKIP_REASON)
            )
        else:
            selected.append((slot, export_kw))
            drawn = min(energy, available)
            available -= drawn
            spent += energy - drawn
    selected.reverse()
    return selected, tuple(reversed(skips)), spent


def _export_intent(
    event: FlexibilityEvent,
    cfg: PlannerConfig,
    window_start: datetime,
    selected: list[tuple[_EventSlot, float]],
) -> ExportIntent:
    starts = tuple(slot.start for slot, _ in selected)
    powers = tuple(power for _, power in selected)
    return ExportIntent(
        event_identity=event.identity,
        window_start=window_start,
        window_end=starts[-1] + timedelta(minutes=30),
        planned_export_kw=min(powers),
        dno_export_limit_kw=cfg.dno_export_limit_kw,
        selected_slots=starts,
        slot_export_kw=powers,
    )


_DAYLIGHT_REFUSAL = (
    "Skipped paid slot: today's solar isn't forecast, so in daylight the fixed "
    "discharge command can't be checked against the DNO limit."
)


def _same_day_export_plan(
    inputs: PlannerInputs,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    event: FlexibilityEvent,
    horizon_start: datetime,
    now: datetime,
    load_slots: tuple[float, ...],
) -> tuple[
    ExportIntent | None,
    tuple[ExportSkip, ...],
    str | None,
    float | None,
    datetime | None,
]:
    """Plan an export event that ends before tonight's horizon start (#198).

    The horizon begins at tonight's charge window, so an event later today lies
    before it, after last night's charge. It is funded from the live SoC: house
    load from now through the event, plus the buffered load from the event end
    until the window opens, stays in the battery. Only what is left of the slot
    in progress counts, so a replan during the event keeps exporting.

    Today's solar isn't forecast: the horizon's profile is tomorrow's. Night
    looks the same on both days whatever the weather, so a slot is refused
    wherever tomorrow's forecast shows solar, and the rest are checked against
    the DNO limit with none. Solar never counts towards funding.

    Today's prices aren't in the schedule either, so buying back a spent
    post-event reserve is priced at the standard rate.
    """
    n = len(load_slots)
    solar_slots = inputs.solar_slots or (0.0,) * n

    def in_event(start: datetime) -> bool:
        return event.start <= start and start + timedelta(minutes=30) <= event.end

    # The horizon repeats one day's profile from the window start, so the slot
    # k half-hours before it shares its time of day with horizon slot n - k.
    day = [
        (horizon_start - timedelta(minutes=30 * k), load_slots[n - k], solar_slots[n - k])
        for k in range(n, 0, -1)
    ]
    # (start, load kWh, solar kWh, hours left) for each slot not yet over.
    remaining = [
        (start, load, solar, min(0.5, (start + timedelta(minutes=30) - now).total_seconds() / 3600))
        for start, load, solar in day
        if start + timedelta(minutes=30) > now
    ]
    slots = [
        _EventSlot(start, 0.0, load * 2, hours, _DAYLIGHT_REFUSAL if solar > 1e-9 else None)
        for start, load, solar, hours in remaining
        if in_event(start)
    ]
    if not slots:
        return None, (
            ExportSkip(
                event.start,
                "Skipped paid event: no complete slot of it is left before tonight's "
                "charge window.",
            ),
        ), None, None, None

    event_end = slots[-1].start + timedelta(minutes=30)
    through_event = sum(
        load * hours * 2 for start, load, _, hours in remaining if start < event_end
    )
    post_event = sum(load for start, load, _, _ in remaining if start >= event_end)
    usable_now = max(0.0, cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0)
    buffered_post_energy = post_event * (1.0 + cfg.buffer_pct / 100.0)
    post_energy = buffered_post_energy
    available = max(0.0, usable_now - through_event - post_energy)
    reserve = _Reserve(
        energy=min(post_energy, max(0.0, usable_now - through_event)),
        event_rate=event.rate_gbp_kwh,
        efficiency=cfg.charge_efficiency if cfg.charge_efficiency > 0 else 1.0,
        price_for=lambda _shortfall: schedule.standard_rate,
    )
    selected, skips, spent = _select_export_suffix(slots, available, cfg, schedule, reserve)
    house_energy_by_start = {slot.start: slot.load_kw * slot.hours for slot in slots}
    needed_pct, needed_at = _export_soc_needed(
        slots, selected, skips, house_energy_by_start, buffered_post_energy, cfg
    )
    if not selected:
        return None, skips, None, needed_pct, needed_at

    # Once the event is under way, keep the start it was armed with: dropping a
    # finished slot changes nothing on the inverter but costs a Slot 1 write.
    window_start = selected[0][0].start
    if window_start == slots[0].start:
        window_start = next(start for start, _, _ in day if in_event(start))
    funding = reserve.funding(spent) if spent > 1e-9 else None
    return _export_intent(event, cfg, window_start, selected), skips, funding, needed_pct, needed_at


def _event_export_plan(
    inputs: PlannerInputs,
    cfg: PlannerConfig,
    schedule: TariffSchedule,
    net: list[float],
    charge_limit: float,
) -> tuple[
    ExportIntent | None,
    tuple[Reservation, ...],
    tuple[ExportSkip, ...],
    str | None,
    float | None,
    datetime | None,
    bool,
]:
    """Reserve and select one contiguous, fully fundable Axle export suffix.

    Work backwards from the event end.  That preserves the energy needed from
    the event end to the next cheap slot before admitting an export slot, and a
    suffix maps directly to an inverter's single timed-discharge window.

    The post-event reservation funds a slot only when buying the shortfall back
    costs less than the event pays (#207); the funding value then says so.
    The last value says whether ``charge_limit`` (what the charge window can
    add), not the target cap, limited the energy for the event (#208).
    """
    event = inputs.flexibility_event
    if event is None or event.direction != "export" or inputs.horizon_start is None:
        return None, (), (), None, None, None, False
    if (
        inputs.now is not None
        and inputs.load_slots is not None
        and event.end <= inputs.horizon_start
    ):
        export, skips, funding, needed_pct, needed_at = _same_day_export_plan(
            inputs, cfg, schedule, event, inputs.horizon_start, inputs.now, inputs.load_slots
        )
        return export, (), skips, funding, needed_pct, needed_at, False

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
        ), None, None, None, False

    # If any event slot overlaps the physical timed-charge window, export
    # outranks discretionary charging. Only energy already in the battery may
    # fund that event; the caller will emit a zero-charge intent below.
    n_charge_slots = int(schedule.window_hours * 2)
    charge_window_conflict = any(i < n_charge_slots for i in event_slots)
    usable_capacity = max(0.0, cfg.capacity_kwh * (cfg.target_cap - cfg.min_soc) / 100.0)
    charge_capped = False
    if charge_window_conflict:
        usable_capacity = max(
            0.0, cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
        )
    else:
        # The window can't always fill to the cap: 62.5 A for six hours adds
        # less than a deep battery's headroom.
        reachable = (
            max(
                0.0,
                cfg.capacity_kwh * (inputs.soc_now - cfg.min_soc) / 100.0
                - inputs.pre_window_drain_kwh,
            )
            + charge_limit
        )
        charge_capped = reachable < usable_capacity - 1e-9
        usable_capacity = min(usable_capacity, reachable)

    # The named post-event reservation is computed first and spent to deliver
    # an event only when buying it back pays (#207).  Like the existing
    # reservation, it carries the planner safety buffer.
    after_event = event_slots[-1] + 1
    target_slot = next(
        (i for i in range(after_event, len(net)) if schedule.cheap_fracs[i] > 1e-9),
        len(net),
    )
    post_need = sum(
        (1.0 - schedule.cheap_fracs[i]) * net[i] for i in range(after_event, target_slot)
    )
    buffered_post_energy = post_need * (1.0 + cfg.buffer_pct / 100.0)
    post_energy = min(buffered_post_energy, usable_capacity)

    def shortfall_price(shortfall: float) -> float:
        # The battery serves the slots after the event in order and runs dry at
        # the end of the stretch, so a shortfall is bought in its last slots.
        worst = 0.0
        for i in reversed(range(after_event, target_slot)):
            need = (1.0 - schedule.cheap_fracs[i]) * net[i]
            if need <= 1e-9:
                continue
            worst = max(worst, schedule.prices[i])
            shortfall -= need
            if shortfall <= 1e-9:
                break
        return worst

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
    reserve = _Reserve(
        energy=min(post_energy, max(0.0, usable_capacity - house_through_event_energy)),
        event_rate=event.rate_gbp_kwh,
        efficiency=cfg.charge_efficiency if cfg.charge_efficiency > 0 else 1.0,
        price_for=shortfall_price,
    )
    solar_slots = inputs.solar_slots or (0.0,) * len(net)
    slots = [
        _EventSlot(
            inputs.horizon_start + timedelta(minutes=30 * i),
            solar_slots[i] * cfg.solar_haircut_k * 2,
            inputs.load_slots[i] * 2 if inputs.load_slots is not None else 0.0,
        )
        for i in event_slots
    ]
    selected, skips, spent = _select_export_suffix(slots, available, cfg, schedule, reserve)
    house_energy_by_start = {
        slot.start: net[i] for slot, i in zip(slots, event_slots, strict=True)
    }
    needed_pct, needed_at = _export_soc_needed(
        slots, selected, skips, house_energy_by_start, buffered_post_energy, cfg
    )
    if not selected:
        return None, (), skips, None, needed_pct, needed_at, charge_capped

    post_energy -= spent
    target_time = inputs.horizon_start + timedelta(minutes=30 * target_slot)
    reason = (
        f"Reserving {post_energy:.2f} kWh after the Axle event to cover forecast house "
        f"load until the {target_time.strftime('%H:%M')} cheap slot."
        if target_slot < len(net)
        else (
            f"Reserving {post_energy:.2f} kWh after the Axle event through "
            "the planning horizon."
        )
    )
    if spent > 1e-9:
        reason += f" The other {spent:.2f} kWh funds the paid export and is bought back."
    post = Reservation(
        name="reach-next-cheap-slot",
        target_slot=target_slot,
        energy_kwh=post_energy,
        obligation_kind="reach-next-cheap-slot",
        reason=reason,
        target_time=target_time,
    )

    export = _export_intent(event, cfg, selected[0][0].start, selected)
    event_energy = house_through_event_energy + sum(power * 0.5 for _, power in selected)
    event_reservation = Reservation(
        name="axle-export-event",
        target_slot=event_slots[slots.index(selected[-1][0])] + 1,
        energy_kwh=event_energy,
        obligation_kind="axle-export-event",
        reason=(
            f"Reserving {event_energy:.2f} kWh for forecast house load before and during "
            "paid Axle export from "
            f"{export.window_start.strftime('%H:%M')} "
            f"to {export.window_end.strftime('%H:%M')}, while protecting house load."
        ),
        target_time=export.window_end,
    )
    funding = reserve.funding(spent) if spent > 1e-9 else None
    return (
        export,
        (event_reservation, post),
        skips,
        funding,
        needed_pct,
        needed_at,
        charge_capped,
    )


def _soc_path(
    horizon_start: datetime,
    net: list[float],
    schedule: TariffSchedule,
    export: ExportIntent | None,
    target_soc: float,
    cfg: PlannerConfig,
) -> tuple[tuple[datetime, float], ...]:
    """Expected SoC at each slot start from the window end, if the plan holds.

    Starts at ``target_soc`` when the window closes, then drains each slot's
    uncovered house load, or all of it plus the export in a selected export
    slot. Never below ``min_soc``.
    """
    exported = (
        {start: power * 0.5 for start, power in
         zip(export.selected_slots, export.slot_export_kw, strict=True)}
        if export is not None
        else {}
    )
    path: list[tuple[datetime, float]] = []
    soc = target_soc
    for i in range(int(schedule.window_hours * 2), len(net)):
        start = horizon_start + timedelta(minutes=30 * i)
        path.append((start, soc))
        drain = (
            net[i] + exported[start]
            if start in exported
            else (1.0 - schedule.cheap_fracs[i]) * net[i]
        )
        soc = max(cfg.min_soc, soc - drain / cfg.capacity_kwh * 100.0)
    return tuple(path)


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
    export_funding: str | None = None
    export_soc_needed_pct: float | None = None
    export_soc_needed_at: datetime | None = None
    overnight_charge_capped = False
    charge_limit = _window_charge_limit(inputs, cfg)
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
        (
            export,
            event_reservations,
            export_skips,
            export_funding,
            export_soc_needed_pct,
            export_soc_needed_at,
            overnight_charge_capped,
        ) = _event_export_plan(inputs, cfg, schedule, net, charge_limit)
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
    # A same-day export (#198) also spends battery before the window opens.
    same_day_export_kwh = (
        sum(power * 0.5 for power in export.slot_export_kw)
        if export is not None
        and inputs.horizon_start is not None
        and export.window_end <= inputs.horizon_start
        else 0.0
    )
    usable_at_window = usable_now - inputs.pre_window_drain_kwh - same_day_export_kwh
    # ...and frees the same room under the cap for tonight's charge.
    headroom += same_day_export_kwh
    reservation_need = sum(reservation.energy_kwh for reservation in reservations)
    export_overlaps_charge = (
        export is not None
        and inputs.horizon_start is not None
        and any(
            0
            <= int((start - inputs.horizon_start).total_seconds() // 1800)
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
        required = min(headroom, charge_limit)
    else:
        # The reservation only reaches the next cheap slot, which assumes that
        # slot can refill the battery. A short daytime dispatch cannot, so it
        # is a floor on the overnight buy, not the whole obligation — the
        # buffered horizon deficit still stands.
        obligation = max(reservation_need, buffered_deficit) if reservations else buffered_deficit
        required = _clamp(
            obligation - usable_at_window,
            0.0,
            min(headroom, charge_limit),
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
                if 0 <= slot < len(schedule.export_prices)
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

    soc_path: tuple[tuple[datetime, float], ...] = ()
    if (
        inputs.load_slots is not None
        and inputs.horizon_start is not None
        and cfg.capacity_kwh > 0
        and not export_overlaps_charge
    ):
        soc_path = _soc_path(inputs.horizon_start, net, schedule, export, target_soc, cfg)

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
        # Inputs built outside gather_inputs (backtest, tests) set no unrated list;
        # their dispatches were never rated, so they are the unrated set.
        unrated_holds=_controlled_windows(
            inputs.unrated_dispatches or inputs.dispatches,
            cfg.window_start,
            cfg.window_end,
        ),
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
        export_funding=export_funding,
        export_soc_needed_pct=export_soc_needed_pct,
        export_soc_needed_at=export_soc_needed_at,
        overnight_charge_capped=overnight_charge_capped,
        soc_path=soc_path,
    )
