"""One place the whole plan pipeline runs: gather → schedule → compute.

Every surface that describes "tonight's plan" — the daily scheduler, the CLI,
the agent tools, the NL copilot and the offline parser — goes through
``current_plan`` so they all report the *same* plan the daemon applies. Skipping
the tariff schedule silently reverts ``compute_plan`` to the fixed two-rate
provider, so on a ``dynamic``/``octopus_intelligent`` install a caller that
forgot it would describe a different plan than the one applied (#91). Threading
the schedule here, once, removes that whole class of drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.sources import build_schedule, gather_inputs
from ha_spark.energy.tariff import TariffSchedule
from ha_spark.ha.rest import HomeAssistantRest


@dataclass(frozen=True)
class PlanRun:
    """A computed plan plus the inputs, config, schedule and forecast source it
    came from, so callers needn't re-derive any of them."""

    plan: ChargePlan
    inputs: PlannerInputs
    cfg: PlannerConfig
    schedule: TariffSchedule
    load_source: str


async def current_plan(settings: Settings, rest: HomeAssistantRest) -> PlanRun:
    """Compute tonight's plan under the configured tariff provider.

    The single entry point for "what is the plan right now": reads live HA
    state, selects the tariff provider, builds its schedule, and runs the
    deterministic planner against it. The caller owns ``rest`` — the scheduler
    and CLI reuse it to apply the resulting plan afterwards.
    """
    inputs, cfg, load_source = await gather_inputs(settings, rest)
    schedule = build_schedule(settings, inputs, cfg)
    plan = compute_plan(inputs, cfg, schedule)
    return PlanRun(
        plan=plan, inputs=inputs, cfg=cfg, schedule=schedule, load_source=load_source
    )
