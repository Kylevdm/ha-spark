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
from datetime import tzinfo

from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan, LoadForecast, PlannerConfig, PlannerInputs
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.solar_source import SolarSource
from ha_spark.energy.sources import build_schedule, gather_inputs
from ha_spark.energy.tariff import TariffSchedule
from ha_spark.ha.rest import HomeAssistantRest


@dataclass(frozen=True)
class PlanRun:
    """A computed plan plus the inputs, config, schedule and forecast it
    came from, so callers needn't re-derive any of them."""

    plan: ChargePlan
    inputs: PlannerInputs
    cfg: PlannerConfig
    schedule: TariffSchedule
    load_source: str
    # The load forecast the plan was sized from: its unrotated 48-value
    # day profile (slot-of-day order) and its served date (#397), never the
    # horizon-order ``inputs.load_slots``.
    forecast: LoadForecast
    # The household zone the inputs were read in, for formatting plan times
    # (horizon instants are UTC).
    tz: tzinfo | None = None


@dataclass(frozen=True)
class StoredPlanRun:
    """A plan run the daemon acted on, kept beside its plan for view-only
    readers (#496).

    ``ev_hold_active`` is what ``run_once`` observed on a local copy of the
    charge intent: it is recorded here only, never fed back into the stored
    ``ChargePlan``, the applied-plan baseline or any write.
    """

    run: PlanRun
    ev_hold_active: bool


async def current_plan(
    settings: Settings,
    rest: HomeAssistantRest,
    *,
    soc: SocMeasurement | None = None,
    source: SolarSource,
) -> PlanRun:
    """Compute tonight's plan under the configured tariff provider.

    The single entry point for "what is the plan right now": reads live HA
    state, selects the tariff provider, builds its schedule, and runs the
    deterministic planner against it. The caller owns ``rest`` — the scheduler
    and CLI reuse it to apply the resulting plan afterwards. ``soc`` is the
    daemon tick's checked measurement, reused as-is so the plan carries the
    exact observation the loop made (no independent reread). ``source`` is
    the solar source context every plan must state explicitly (#519).
    """
    inputs, cfg, forecast = await gather_inputs(settings, rest, soc=soc, source=source)
    schedule = build_schedule(settings, inputs, cfg)
    plan = compute_plan(inputs, cfg, schedule)
    return PlanRun(
        plan=plan, inputs=inputs, cfg=cfg, schedule=schedule,
        load_source=forecast.source, forecast=forecast,
        tz=inputs.now.tzinfo if inputs.now is not None else None,
    )
