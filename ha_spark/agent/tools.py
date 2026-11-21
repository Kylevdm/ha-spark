"""Protocol-agnostic agent tool core.

Each function takes ``Settings`` and returns a pydantic model. The FastAPI
routes and the FastMCP server both call these; a future cloud-inference tier
would too. No HTTP/transport code lives here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from ha_spark.authority import operating_mode
from ha_spark.config import Settings
from ha_spark.daemon_channel import REFUSED, DaemonChannel, Line
from ha_spark.energy.axle_events import window_evidence
from ha_spark.energy.context import ContextStore
from ha_spark.energy.energy_ledger import EnergyLedger
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.orchestrator import orchestrate
from ha_spark.energy.plan_run import current_plan
from ha_spark.energy.publish import plan_to_payload
from ha_spark.energy.readiness_measurements import HALF_HOUR
from ha_spark.energy.report import format_plan
from ha_spark.energy.solar_source import NATIVE_PREVIEW_NOTE, solar_source_from_store
from ha_spark.energy.sources import gather_inputs
from ha_spark.energy.stats import (
    CostInputs,
    RangeStats,
    RangeTotals,
    Resolution,
    range_bounds,
    summarise,
)
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import run_health
from ha_spark.logging import get_logger

log = get_logger(__name__)


class PlanResult(BaseModel):
    plan: list[dict[str, object]]
    generated_at: str | None
    text: str = ""
    actions: list[Line] = Field(default_factory=list)
    refusal: str | None = None


class StateResult(BaseModel):
    inputs: dict[str, object]
    operating_mode: str
    # {id, type, driver} per configured device — no entity IDs/secrets.
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


class StatisticsResult(BaseModel):
    version: int = 1
    range: str
    at: str
    start: str
    end: str
    resolution: str
    timezone: str
    buckets: list[dict[str, object]]
    range_totals: dict[str, object]
    axle: dict[str, object]


STATISTICS_RANGES = ("day", "week", "month", "year", "all")
INVALID_RANGE = "invalid statistics range"
INVALID_AT = "at must be a valid YYYY-MM-DD date"


class StatisticsInputError(ValueError):
    """Invalid ``range``/``at``; the message is one of the fixed texts above, never the input."""


class StatisticsUnavailable(Exception):
    """Ledger or Axle history could not be read; callers fail closed."""


def _serialize(obj: object) -> object:
    """Make a payload JSON-safe: non-finite floats become None."""
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    return obj


def _totals_payload(totals: RangeTotals) -> dict[str, object]:
    """Same keys as ``api/history.py:_totals_payload`` (not imported: private helper)."""
    return {
        "components_kwh": totals.energy.components_kwh,
        "house_load_kwh": totals.energy.house_load_kwh,
        "base_load_kwh": totals.energy.base_load_kwh,
        "actual_cost": totals.actual_cost,
        "no_battery_cost": totals.no_battery_cost,
        "battery_savings": totals.battery_savings,
        "covered_slots": totals.covered_slots,
        "priced_slots": totals.priced_slots,
        "total_slots": totals.total_slots,
        "lower_resolution": totals.lower_resolution,
        "energy_coverage": totals.energy_coverage,
        "cost_coverage": totals.cost_coverage,
    }


def _cost_inputs_payload(inputs: CostInputs) -> dict[str, object]:
    """Same keys as ``api/history.py:add_statistics``'s ``cost_inputs``."""
    return {
        "import_rate": inputs.rates.import_rate,
        "export_rate": inputs.rates.export_rate,
        "import_session": inputs.rates.import_session,
        "grid_import_kwh": inputs.grid_import_kwh,
        "grid_export_kwh": inputs.grid_export_kwh,
        "house_load_kwh": inputs.house_load_kwh,
        "solar_kwh": inputs.solar_kwh,
        "free_import_kwh": inputs.free_import_kwh,
        "no_battery_import_kwh": inputs.no_battery_import_kwh,
        "no_battery_export_kwh": inputs.no_battery_export_kwh,
        "no_battery_free_import_kwh": inputs.no_battery_free_import_kwh,
        "axle_earnings": inputs.axle_earnings,
    }


def _statistics_payload(
    stats: RangeStats, kind: str, at: str, start: datetime, end: datetime, resolution: Resolution,
    tz: ZoneInfo,
) -> dict[str, object]:
    """Build the version-1 JSON contract for the statistics tool (phases 13/14 build on it)."""
    buckets: list[dict[str, object]] = []
    for bucket in stats.buckets:
        entry: dict[str, object] = {"start": bucket.start.isoformat()}
        entry.update(_totals_payload(bucket.totals))
        if bucket.slot is not None:
            entry["flows"] = {
                name: {"kwh": flow.kwh, "provenance": flow.provenance}
                for name in bucket.totals.energy.components_kwh
                if (flow := bucket.slot.components.get(name)) is not None
            }
            entry["negative_clamped"] = bucket.slot.negative_clamped
        if bucket.cost is not None:
            entry["cost_reason"] = bucket.cost.reason
            if bucket.cost.inputs is not None:
                entry["cost_inputs"] = _cost_inputs_payload(bucket.cost.inputs)
        buckets.append(entry)
    axle = stats.axle
    return {
        "version": 1,
        "range": kind,
        "at": at,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "resolution": resolution,
        "timezone": tz.key,
        "buckets": buckets,
        "range_totals": _totals_payload(stats.totals),
        "axle": {
            "delivery_rate": axle.delivery_rate,
            "earnings_by_day": [
                {"date": day.isoformat(), "earnings_gbp": earnings}
                for day, earnings in stats.axle_earnings_by_day
            ],
            "events": [
                {
                    "start": outcome.event.start.isoformat(),
                    "end": outcome.event.end.isoformat(),
                    "outcome": outcome.outcome,
                    "delivered_kwh": outcome.delivered_kwh,
                    "earnings_gbp": outcome.earnings_gbp,
                }
                for outcome in axle.events
            ],
        },
    }


def _rest(settings: Settings) -> HomeAssistantRest:
    return HomeAssistantRest(settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout)


async def get_plan(settings: Settings) -> PlanResult:
    source = solar_source_from_store(settings)
    async with _rest(settings) as rest:
        run = await current_plan(settings, rest, source=source)
    plan = run.plan
    entities = [
        {"entity_id": eid, "state": value, "attributes": attrs}
        for eid, value, attrs in plan_to_payload(plan, settings)
    ]
    text = format_plan(plan, run.load_source, forecast_date=run.forecast.target_date, tz=run.tz)
    if source.kind == "native":
        text += "\n" + NATIVE_PREVIEW_NOTE
    return PlanResult(
        plan=entities, generated_at=datetime.now(UTC).isoformat(), text=text,
    )


async def get_state(settings: Settings) -> StateResult:
    source = solar_source_from_store(settings)
    async with _rest(settings) as rest:
        inputs, _cfg, _forecast = await gather_inputs(settings, rest, source=source)
    data = jsonable_encoder(dataclasses.asdict(inputs))
    data.pop("load_slots", None)  # large; available via get_forecast
    if source.kind == "native":
        data["solar_note"] = NATIVE_PREVIEW_NOTE
    devices = [
        {"id": d.id, "type": d.type, "driver": d.driver}
        for d in settings.devices
    ]
    return StateResult(inputs=data, devices=devices, operating_mode=operating_mode(settings))


async def get_forecast(settings: Settings) -> ForecastResult:
    """The served day's load forecast: 48 day-order slot values plus its date."""
    async with _rest(settings) as rest:
        run = await current_plan(settings, rest, source=solar_source_from_store(settings))
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


async def get_statistics(settings: Settings, range_: str, at: str | None) -> StatisticsResult:
    """Measured energy, cost, battery savings, Axle earnings and coverage.

    Mirrors ``cli._cmd_stats``: household-calendar bounds (``all`` runs from the
    first ledger slot to now; ``at`` is then only validated), one slot of pricing
    context, Axle evidence, the same resolution map. Raises
    ``StatisticsInputError`` (fixed text) or ``StatisticsUnavailable``; never a
    partial result.
    """
    if range_ not in STATISTICS_RANGES:
        raise StatisticsInputError(INVALID_RANGE)
    tz = load_timezone(settings.timezone)
    now = datetime.now(tz)
    if at is None:
        day = now.date()
    else:
        try:
            day = date.fromisoformat(at)
        except (ValueError, OverflowError):
            raise StatisticsInputError(INVALID_AT) from None
        if day.isoformat() != at:
            raise StatisticsInputError(INVALID_AT)
    try:
        start, end = range_bounds(range_, day, tz)
    except (ValueError, OverflowError):
        raise StatisticsInputError(INVALID_AT) from None
    resolution: Resolution = ("slot" if range_ == "day" else "day" if range_ in ("week", "month")
                              else "month")
    try:
        ledger = EnergyLedger(settings.db_path)
        if range_ == "all":
            start, _ = range_bounds(range_, now.date(), tz, first_slot=await ledger.first_slot())
            end = now.astimezone(UTC)
        rows = await ledger.slots(start - HALF_HOUR, end)
        events, event_rows = await window_evidence(settings.db_path, start, end)
        stats = await asyncio.to_thread(summarise, rows, start, end, tz, resolution,
                                        events, event_rows=event_rows)
        payload = _statistics_payload(stats, range_, at or day.isoformat(), start, end,
                                      resolution, tz)
        return StatisticsResult.model_validate(_serialize(payload))
    except Exception:
        log.warning("Statistics read failed")
        raise StatisticsUnavailable() from None
