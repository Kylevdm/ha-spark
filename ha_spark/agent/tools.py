"""Protocol-agnostic agent tool core.

Each function takes ``Settings`` and returns a pydantic model. The FastAPI
routes and the FastMCP server both call these; a future cloud-inference tier
would too. No HTTP/transport code lives here.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from ha_spark.config import Settings
from ha_spark.energy.context import ContextStore
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import run_health


class PlanResult(BaseModel):
    plan: list[dict[str, object]]
    generated_at: str | None


class StateResult(BaseModel):
    inputs: dict[str, object]


class ForecastResult(BaseModel):
    load_kwh: float
    slots: list[float] | None
    source: str


class PredictionsResult(BaseModel):
    decisions: list[dict[str, object]]


class HealthResult(BaseModel):
    checks: list[dict[str, object]]


class ContextResult(BaseModel):
    facts: list[dict[str, object]]


def _rest(settings: Settings) -> HomeAssistantRest:
    return HomeAssistantRest(settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout)


async def get_plan(settings: Settings) -> PlanResult:
    async with _rest(settings) as rest:
        inputs, cfg, _src = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    return PlanResult(plan=entities, generated_at=datetime.now(UTC).isoformat())


async def get_state(settings: Settings) -> StateResult:
    async with _rest(settings) as rest:
        inputs, _cfg, _src = await gather_inputs(settings, rest)
    data = jsonable_encoder(dataclasses.asdict(inputs))
    data.pop("load_slots", None)  # large; available via get_forecast
    return StateResult(inputs=data)


async def get_forecast(settings: Settings) -> ForecastResult:
    async with _rest(settings) as rest:
        inputs, cfg, source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    slots = list(inputs.load_slots) if inputs.load_slots is not None else None
    return ForecastResult(load_kwh=plan.load_kwh, slots=slots, source=source)


async def get_predictions(settings: Settings) -> PredictionsResult:
    decisions = await orchestrate(settings)
    return PredictionsResult(
        decisions=[jsonable_encoder(dataclasses.asdict(d)) for d in decisions]
    )


async def get_health(settings: Settings) -> HealthResult:
    checks = await run_health(settings)
    return HealthResult(
        checks=[{"name": c.name, "status": c.status.name, "detail": c.detail} for c in checks]
    )


async def get_context(settings: Settings) -> ContextResult:
    async with ContextStore(settings.db_path) as store:
        facts = await store.list_all()
    return ContextResult(facts=[jsonable_encoder(dataclasses.asdict(f)) for f in facts])


async def add_context(
    settings: Settings, kind: str, start_date: date, end_date: date, note: str = ""
) -> ContextResult:
    async with ContextStore(settings.db_path) as store:
        await store.add(kind, start_date, end_date, note=note, source="agent")
    return await get_context(settings)


async def run_plan(settings: Settings) -> PlanResult:
    # Local import: scheduler imports api.server, which imports this module
    # (Task 7) — importing run_once at top level would close that cycle.
    from ha_spark.energy.scheduler import run_once

    plan = await run_once(settings)
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    return PlanResult(plan=entities, generated_at=datetime.now(UTC).isoformat())
