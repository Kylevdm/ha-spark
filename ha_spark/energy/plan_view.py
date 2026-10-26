"""Pure, view-only planner output; never an input to device intent or sizing.

Builders own plan wording and hiding of untrusted SoC-derived values. The API
serialises this output, and the ingress scripts only render it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

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
    kind: Literal["warning"]
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
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class PlanView:
    headline: Headline
    costs: Costs
    lines: tuple[PlanLine, ...]
    slots: tuple[PlanSlot, ...] | None
    soc: tuple[()] | None


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
            (a if a.tzinfo else a.replace(tzinfo=at.tzinfo)).astimezone(UTC) < end
            and lo < (b if b.tzinfo else b.replace(tzinfo=at.tzinfo)).astimezone(UTC)
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
            export_price=(schedule.export_prices[i] if schedule.export_prices
                          else schedule.export_rate),
            cheap_frac=schedule.cheap_fracs[i], load_kwh=inputs.load_slots[i] * remaining,
            solar_kwh=solar[i] * cfg.solar_haircut_k * remaining, actions=tuple(actions),
        ))
    return tuple(result)


def build_plan_view(
    plan: ChargePlan, cfg: PlannerConfig, inputs: PlannerInputs, schedule: TariffSchedule,
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
        lines=() if trusted else (
            PlanLine("warning", None, None,
                     _SOC_WARNINGS.get(plan.soc.status, "SoC reading not trusted")),
        ),
        slots=_slots(plan, cfg, inputs, schedule),
        soc=() if plan.model == "slots" else None,
    )
