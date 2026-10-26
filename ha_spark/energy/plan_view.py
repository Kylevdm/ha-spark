"""Pure, view-only planner output; never an input to device intent or sizing.

Builders own plan wording and hiding of untrusted SoC-derived values. The API
serialises this output, and the ingress scripts only render it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from ha_spark.energy.soc_integrity import SocStatus

if TYPE_CHECKING:
    from ha_spark.energy.models import ChargePlan, PlannerConfig


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


@dataclass(frozen=True)
class PlanView:
    headline: Headline
    costs: Costs
    lines: tuple[PlanLine, ...]
    slots: tuple[()] | None
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


def build_plan_view(plan: ChargePlan, cfg: PlannerConfig) -> PlanView:
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
        slots=() if plan.model == "slots" else None,
        soc=() if plan.model == "slots" else None,
    )
