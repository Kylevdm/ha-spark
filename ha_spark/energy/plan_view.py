"""Pure, view-only planner output; never an input to device intent or sizing.

Builders own plan wording and hiding of untrusted SoC-derived values. The API
serialises this output, and the ingress scripts only render it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from ha_spark.energy.models import _align
from ha_spark.energy.soc_integrity import SocStatus

if TYPE_CHECKING:
    from ha_spark.energy.models import ChargePlan, PlannerConfig, PlannerInputs
    from ha_spark.energy.tariff import TariffSchedule


@dataclass(frozen=True)
class Headline:
    soc_now: float | None
    target_soc: float | None
    required_kwh: float | None
    min_soc: float
    capacity_kwh: float


@dataclass(frozen=True)
class Costs:
    planned: float | None
    baseline: float | None
    export_revenue: float | None


@dataclass(frozen=True)
class PlanLine:
    kind: Literal["charge", "hold", "reserve", "export", "skip", "v2l", "warning"]
    start: datetime | None
    end: datetime | None
    text: str
    kwh: float | None = None


Action = Literal["charge", "hold", "export", "skip", "v2l"]


@dataclass(frozen=True)
class PlanSlot:
    start: datetime
    import_price: float
    export_price: float
    cheap_frac: float
    load_kwh: float
    solar_kwh: float
    # Solcast's estimate before the planner's haircut, on the same slot-0 basis.
    solar_raw_kwh: float
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class SocPoint:
    at: datetime
    soc_pct: float


@dataclass(frozen=True)
class PlanView:
    headline: Headline
    costs: Costs
    lines: tuple[PlanLine, ...]
    slots: tuple[PlanSlot, ...] | None
    soc: tuple[SocPoint, ...] | None


# Fixed wording only: SocMeasurement.reason can contain raw upstream state.
_SOC_WARNINGS = {
    SocStatus.IMPLAUSIBLE: "SoC disagrees with battery energy flow",
    SocStatus.READ_FAILED: "SoC read from Home Assistant failed",
    SocStatus.UNAVAILABLE: "SoC sensor unavailable",
    SocStatus.MALFORMED: "SoC state is not a number",
    SocStatus.NOT_FINITE: "SoC state is not finite",
    SocStatus.OUT_OF_RANGE: "SoC outside 0-100%",
    SocStatus.REPORT_TIME_UNUSABLE: "SoC last_reported is missing or unusable",
    SocStatus.REPORT_TIME_FUTURE: "SoC last_reported is in the future",
    SocStatus.STALE: "SoC report is stale",
    SocStatus.UNCHANGED_TOO_LONG: "SoC unchanged beyond the maximum age despite a live source",
}


def _slots(
    plan: ChargePlan, cfg: PlannerConfig, inputs: PlannerInputs, schedule: TariffSchedule,
) -> tuple[PlanSlot, ...] | None:
    if inputs.load_slots is None:
        return None
    geometry = inputs.geometry_for(cfg)
    if geometry is None or geometry.horizon_start.tzinfo is None:
        return ()  # Clockless legacy inputs have no absolute instants to display.
    n = len(inputs.load_slots)
    if (
        len(geometry.slot_starts) != n or len(schedule.prices) != n
        or len(schedule.cheap_fracs) != n or len(schedule.export_prices) not in (0, n)
    ):
        return ()  # The view must never break planning on a horizon it can't align.
    # The planner zips solar against load non-strictly; a missing slot used no solar.
    solar = tuple(inputs.solar_slots or ())[:n] + (0.0,) * (n - len(inputs.solar_slots or ()))
    selected = plan.charge_intent.export.selected_slots if plan.charge_intent.export else ()
    exports = {at.astimezone(UTC) for at in selected}
    skips = {skip.start.astimezone(UTC) for skip in plan.export_skips}
    refills = {refill.slot for refill in plan.v2l_refills}
    # Naive hold bounds are read on the household clock, as ChargeIntent does.
    clock = inputs.now or geometry.horizon_start
    event_rate = inputs.flexibility_event.rate_gbp_kwh if inputs.flexibility_event else 0.0
    result = []
    for i, at in enumerate(geometry.slot_starts):
        start = at.astimezone(UTC)
        end = start + timedelta(minutes=30)
        remaining = geometry.slot0_remaining_frac if i == 0 else 1.0
        lo = end - timedelta(minutes=30 * remaining)
        actions: list[Action] = []
        if plan.soc.ok:
            # A positive purchase is spread over the remaining window, not
            # over the tariff's cheap set (which can include daytime dispatch).
            if plan.required_kwh > 0 and max(
                lo, geometry.window_start.astimezone(UTC)
            ) < min(end, geometry.window_end.astimezone(UTC)):
                actions.append("charge")
        if any(
            _align(a, clock).astimezone(UTC) < end and lo < _align(b, clock).astimezone(UTC)
            for a, b in plan.charge_intent.holds
        ):
            actions.append("hold")
        if plan.soc.ok:
            if start in exports:
                actions.append("export")
            if start in skips:
                actions.append("skip")
            if i in refills:
                actions.append("v2l")
        result.append(PlanSlot(
            start=start, import_price=schedule.prices[i],
            # As the planner prices paid export: the schedule overlay, else the event rate.
            export_price=(schedule.export_prices[i] if schedule.export_prices
                          else event_rate if start in exports else schedule.export_rate),
            cheap_frac=schedule.cheap_fracs[i], load_kwh=inputs.load_slots[i] * remaining,
            solar_kwh=solar[i] * cfg.solar_haircut_k * remaining,
            solar_raw_kwh=solar[i] * remaining, actions=tuple(actions),
        ))
    return tuple(result)


def v2l_refill_reason(kwh: float, reservation_name: str) -> str:
    """Word both an individual refill and a folded view without changing CLI text."""
    return (f"Refill the house battery with {kwh:.2f} kWh from V2L for "
            f"the {reservation_name} reservation.")


def ev_hold_line(now: datetime | None) -> PlanLine:
    """Describe the recorded observation, never a control input or scheduled end."""
    start = now.astimezone(UTC) if now is not None and now.tzinfo is not None else None
    return PlanLine("hold", start, None,
                    "Hold the house battery while an EV hold is active.")


def _lines(plan: ChargePlan, cfg: PlannerConfig, inputs: PlannerInputs) -> tuple[PlanLine, ...]:
    geometry = inputs.geometry_for(cfg)
    absolute = (plan.model == "slots" and geometry is not None
                and geometry.horizon_start.tzinfo is not None)
    clock = inputs.now or (geometry.horizon_start if geometry is not None else None)

    def instant(at: datetime | None) -> datetime | None:
        if at is None or clock is None or clock.tzinfo is None:
            return None
        return _align(at, clock).astimezone(UTC)

    now = instant(clock)

    def slot_span(index: int) -> tuple[datetime | None, datetime | None]:
        if not absolute or geometry is None or not 0 <= index < len(geometry.slot_starts):
            return None, None
        start = instant(geometry.slot_starts[index])
        if start is None:
            return None, None
        return max(start, now) if now is not None else start, start + timedelta(minutes=30)

    # The second item is the reservation identity, used only to fold V2L refills.
    rows: list[tuple[PlanLine, str]] = []
    for a, b in plan.charge_intent.holds:
        start, end = instant(a), instant(b)
        if now is not None and end is not None and end <= now:
            continue
        if start is not None and now is not None:
            start = max(start, now)
        rows.append((PlanLine("hold", start, end,
                              "Hold the house battery during a smart-charge dispatch."), ""))
    if not plan.soc.ok:
        rows.append((PlanLine("warning", None, None,
                              _SOC_WARNINGS.get(plan.soc.status, "SoC reading not trusted")), ""))
    else:
        if plan.required_kwh > 0:
            start = instant(geometry.window_start) if absolute and geometry else None
            end = instant(geometry.window_end) if absolute and geometry else None
            if start is not None and now is not None:
                start = max(start, now)
            if start is None or end is None or start < end:
                rows.append((PlanLine("charge", start, end,
                                      f"Charge the house battery with {plan.required_kwh:.2f} kWh "
                                      f"to reach {plan.target_soc:.0f}% SoC.",
                                      plan.required_kwh), ""))
        for reservation in plan.reservations:
            target = instant(reservation.target_time)
            if target is None and absolute and geometry is not None:
                if 0 <= reservation.target_slot < len(geometry.slot_starts):
                    target = instant(geometry.slot_starts[reservation.target_slot])
            rows.append((PlanLine("reserve", now if target is not None else None, target,
                                  reservation.reason, reservation.energy_kwh), ""))
        export = plan.charge_intent.export
        if export is not None and len(export.selected_slots) == len(export.slot_export_kw):
            for at, kw in zip(export.selected_slots, export.slot_export_kw, strict=True):
                start = instant(at)
                end = start + timedelta(minutes=30) if start is not None else None
                if start is not None and now is not None:
                    start = max(start, now)
                if start is not None and end is not None and start >= end:
                    continue
                hours = (end - start).total_seconds() / 3600 if start and end else 0.5
                kwh = kw * hours
                rows.append((PlanLine("export", start, end,
                                      f"Export {kwh:.2f} kWh from the house battery.", kwh), ""))
        for skip in plan.export_skips:
            start = instant(skip.start)
            end = start + timedelta(minutes=30) if start else None
            if start is not None and now is not None:
                start = max(start, now)
            if start is None or end is None or start < end:
                rows.append((PlanLine("skip", start, end, skip.reason), ""))
        for refill in plan.v2l_refills:
            if geometry is None or not 0 <= refill.slot < len(geometry.slot_starts):
                continue
            start, end = slot_span(refill.slot)
            if start is not None and end is not None and start >= end:
                continue
            rows.append((PlanLine("v2l", start, end, refill.reason, refill.kwh),
                         refill.reservation))

    kinds = ("warning", "charge", "hold", "reserve", "export", "skip", "v2l")
    folded: list[PlanLine] = []
    for kind in kinds:
        group = sorted((row for row in rows if row[0].kind == kind),
                       key=lambda row: row[0].start or datetime.max.replace(tzinfo=UTC))
        previous_identity = ""
        for line, identity in group:
            previous = folded[-1] if folded and folded[-1].kind == kind else None
            adjacent = (previous is not None and previous.end is not None
                        and previous.end == line.start)
            merge = (kind == "export" or kind in ("skip", "hold")
                     and previous is not None and previous.text == line.text
                     or kind == "v2l" and bool(identity) and identity == previous_identity)
            if adjacent and merge and previous is not None:
                kwh = (previous.kwh or 0) + (line.kwh or 0)
                text = (v2l_refill_reason(kwh, identity) if kind == "v2l" else
                        f"Export {kwh:.2f} kWh from the house battery." if kind == "export"
                        else line.text)
                folded[-1] = PlanLine(line.kind, previous.start, line.end, text,
                                      kwh if line.kwh is not None else None)
            else:
                folded.append(line)
            previous_identity = identity
    return tuple(sorted(folded, key=lambda line: (
        line.kind != "warning", line.start or datetime.max.replace(tzinfo=UTC),
        kinds.index(line.kind),
    )))


def build_plan_view(
    plan: ChargePlan, cfg: PlannerConfig, inputs: PlannerInputs, schedule: TariffSchedule,
    soc: tuple[SocPoint, ...] = (),
) -> PlanView:
    """Describe the computed numbers without changing the control contract."""
    trusted = plan.soc.ok
    return PlanView(
        headline=Headline(
            soc_now=plan.soc_now if trusted else None,
            target_soc=plan.target_soc if trusted else None,
            required_kwh=plan.required_kwh if trusted else None,
            min_soc=cfg.min_soc,
            capacity_kwh=plan.capacity_kwh,
        ),
        costs=Costs(
            planned=plan.planned_cost if trusted else None,
            baseline=plan.baseline_cost if trusted else None,
            export_revenue=plan.export_revenue if trusted else None,
        ),
        lines=_lines(plan, cfg, inputs),
        slots=_slots(plan, cfg, inputs, schedule),
        soc=(soc if trusted else ()) if plan.model == "slots" else None,
    )
