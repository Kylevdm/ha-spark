"""Protocol-agnostic agent tool core.

Each function takes ``Settings`` and returns a pydantic model. The FastAPI
routes and the FastMCP server both call these; a future cloud-inference tier
would too. No HTTP/transport code lives here.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from ha_spark.config import Settings
from ha_spark.daemon_channel import REFUSED, DaemonChannel, Line
from ha_spark.energy.context import ContextStore
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import run_health


class PlanResult(BaseModel):
    plan: list[dict[str, object]]
    generated_at: str | None
    text: str = ""
    actions: list[Line] = Field(default_factory=list)
    refusal: str | None = None


class StateResult(BaseModel):
    inputs: dict[str, object]
    # {id, type, driver, control} per configured device — no entity IDs/secrets.
    devices: list[dict[str, str]] = Field(default_factory=list)


class ForecastResult(BaseModel):
    load_kwh: float
    # 48 day-order slot kWh (slot-of-day order) for the forecast's date —
    # never the horizon-order ``inputs.load_slots`` (#397).
    slots: list[float] | None
    source: str
    # ISO date of the served day the forecast targets; None on forecasts
    # built without one.
    date: str | None = None


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
        run = await current_plan(settings, rest)
    plan = run.plan
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    return PlanResult(
        plan=entities, generated_at=datetime.now(UTC).isoformat(),
        text=format_plan(plan, run.load_source, forecast_date=run.forecast.target_date, tz=run.tz),
    )


async def get_state(settings: Settings) -> StateResult:
    async with _rest(settings) as rest:
        inputs, _cfg, _forecast = await gather_inputs(settings, rest)
    data = jsonable_encoder(dataclasses.asdict(inputs))
    data.pop("load_slots", None)  # large; available via get_forecast
    devices = [
        {"id": d.id, "type": d.type, "driver": d.driver, "control": d.control.value}
        for d in settings.devices
    ]
    return StateResult(inputs=data, devices=devices)


async def get_forecast(settings: Settings) -> ForecastResult:
    """The served day's load forecast: 48 day-order slot values plus its date."""
    async with _rest(settings) as rest:
        run = await current_plan(settings, rest)
    forecast = run.forecast
    return ForecastResult(
        load_kwh=run.plan.load_kwh,
        slots=list(forecast.slots) if forecast.slots is not None else None,
        source=run.load_source,
        date=forecast.target_date.isoformat() if forecast.target_date is not None else None,
    )


async def get_predictions(settings: Settings) -> PredictionsResult:
    decisions = await orchestrate(settings)
    return PredictionsResult(decisions=[jsonable_encoder(dataclasses.asdict(d)) for d in decisions])


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


async def run_plan(settings: Settings, *, executor: DaemonChannel | None = None) -> PlanResult:
    """Enqueue a fixed replan operation, never device parameters or identity."""
    if executor is None:
        result = await get_plan(settings)
        result.refusal = REFUSED
        return result
    reply = await executor.submit("replan")
    return PlanResult(
        plan=reply.plan, generated_at=datetime.now(UTC).isoformat(),
        text=reply.text, actions=reply.lines, refusal=reply.error,
    )
