"""Append-only slot and guard evidence for readiness; never actuates hardware.

Slots are captured only on the boundary tick, from the revision then in force.
A missed boundary or a horizon without that instant stays a gap. Reads (including
CLI diagnostics) open SQLite read-only and do not initialise a missing ledger.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.energy.forecast import forecast_model_tag, load_timezone
from ha_spark.energy.plan_run import PlanRun
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.sources import parse_time
from ha_spark.energy.supply_guard import throttled_rate_w
from ha_spark.energy.tariff import _in_overnight_window
from ha_spark.energy.topology import EffectiveLimits
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS readiness_slots (
    slot_start TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readiness_guard (
    observed_at TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class PlanRevision:
    """The computed revision in force, including its immutable planning provenance."""

    revision_id: str
    issued_at: datetime
    run: PlanRun
    target_w: float | None
    config_generation: int


class SlotRecord(BaseModel):
    """Replay inputs captured together, never reconstructed from a later plan."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    slot_start: datetime
    operating_mode: str
    revision_id: str
    issued_at: datetime
    charge_window_fraction: float = Field(ge=0, le=1)
    charge_current_a: float | None
    export_kw: float
    hold_state: Literal["held", "not_held", "unknown"]
    v2l_available: bool
    start_energy_kwh: float | None
    capacity_kwh: float
    min_soc: float
    target_cap: float
    voltage_v: float
    charge_efficiency: float
    max_current_a: float
    battery_discharge_ceiling_kw: float
    import_rate: float
    export_rate: float
    event_rate: float | None
    forecast_model: str
    config_generation: int
    limits_source: str | None = None
    effective_limits: EffectiveLimits | None = None


class GuardRecord(BaseModel):
    """One Observe in-window sample, including invalid evidence for coverage."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    observed_at: datetime
    slot_start: datetime
    revision_id: str | None
    sample_valid: bool
    measured_import_w: float | None
    setpoint_w: float | None
    target_w: float | None
    throttle_w: float | None
    breach: bool | None


# How late the first tick of a slot may land and still capture that slot.
BOUNDARY_TOLERANCE = timedelta(minutes=5)


def slot_start(now: datetime) -> datetime:
    """Half-hour boundary as an unambiguous UTC instant (also on DST days)."""
    return now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0).astimezone(UTC)


class ReadinessLedger:
    """Small append-only SQLite ledger, sharing the forecast database file."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def record(self, row: SlotRecord | GuardRecord) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            if isinstance(row, SlotRecord):
                await db.execute(
                    "INSERT INTO readiness_slots VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (row.slot_start.astimezone(UTC).isoformat(), row.model_dump_json()),
                )
            else:
                await db.execute(
                    "INSERT INTO readiness_guard VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (row.observed_at.astimezone(UTC).isoformat(), row.model_dump_json()),
                )
            await db.commit()

    async def slots(self, start: datetime, end: datetime) -> list[SlotRecord]:
        """Read captured slots in [start, end), without creating or migrating a DB."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return []
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'readiness_slots'"
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute(
                "SELECT payload FROM readiness_slots WHERE slot_start >= ? AND slot_start < ? "
                "ORDER BY slot_start",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ) as cursor:
                return [SlotRecord.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def first_slot_start(self) -> datetime | None:
        """Earliest captured slot, one row, without creating or migrating a DB."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return None
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'readiness_slots'"
            ) as cursor:
                if await cursor.fetchone() is None:
                    return None
            async with db.execute(
                "SELECT slot_start FROM readiness_slots ORDER BY slot_start LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
                return datetime.fromisoformat(row[0]) if row else None

    async def guards(self) -> list[GuardRecord]:
        """Read guard samples in chronological order."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return []
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'readiness_guard'"
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute("SELECT payload FROM readiness_guard ORDER BY observed_at") as c:
                return [GuardRecord.model_validate_json(row[0]) for row in await c.fetchall()]


async def record_slot(
    db_path: str, now: datetime, revision: PlanRevision | None, mode: str,
    soc: SocMeasurement | None, *, ev_hold_active: bool | None = None,
) -> None:
    """Capture the boundary tick only; errors never break a daemon tick.

    The daemon calls this on the first tick of each slot only. Ticks drift past
    the minute (60 s sleep plus the tick's work), so that first tick is eligible
    within ``BOUNDARY_TOLERANCE``; a restart later in the slot stays a gap.
    """
    if revision is not None and revision.run.tz is not None:
        now = now.astimezone(revision.run.tz)
    start = slot_start(now)
    if revision is None or now.astimezone(UTC) - start >= BOUNDARY_TOLERANCE:
        return
    try:
        run = revision.run
        cfg, inputs, schedule = run.cfg, run.inputs, run.schedule
        geometry = inputs.geometry_for(cfg)
        if geometry is None:
            return
        starts = tuple(t.astimezone(UTC) for t in geometry.slot_starts)
        if start not in starts:
            return
        i = starts.index(start)
        if i >= len(schedule.prices):
            return
        topology, limits = run.plan.topology, run.plan.effective_limits
        voltage = topology.banks[0].nominal_voltage_v if topology is not None else 0.0
        intent = run.plan.charge_intent
        held = intent.hold_active(start)
        if ev_hold_active is not None:
            held = ev_hold_active or any(s <= start < e for s, e in intent.holds)
        export = intent.export
        export_kw = 0.0
        if export is not None and start in export.selected_slots:
            export_kw = export.slot_export_kw[export.selected_slots.index(start)]
        event = inputs.flexibility_event
        # Only an export event is priced into the schedule (tariff._event_export_prices).
        event_rate = (
            event.rate_gbp_kwh
            if event is not None and event.direction == "export"
            and event.start < start + timedelta(minutes=30) and event.end > start
            else None
        )
        tz = run.tz or (inputs.now.tzinfo if inputs.now is not None else UTC)
        local_day = start.astimezone(tz).date()
        overlap = timedelta()
        for day in (local_day - timedelta(days=1), local_day):
            end_day = day if cfg.window_start < cfg.window_end else day + timedelta(days=1)
            window_start = datetime.combine(day, cfg.window_start, tz).astimezone(UTC)
            window_end = datetime.combine(end_day, cfg.window_end, tz).astimezone(UTC)
            overlap += max(
                timedelta(),
                min(start + timedelta(minutes=30), window_end) - max(start, window_start),
            )
        await ReadinessLedger(db_path).record(SlotRecord(
            slot_start=start, operating_mode=mode, revision_id=revision.revision_id,
            issued_at=revision.issued_at,
            charge_window_fraction=overlap / timedelta(minutes=30),
            charge_current_a=(
                revision.target_w / voltage
                if revision.target_w is not None and voltage > 0 else None
            ),
            export_kw=export_kw,
            hold_state=("held" if held else "not_held") if intent.hold_trusted else "unknown",
            v2l_available=inputs.car_plugged_in,
            start_energy_kwh=(
                cfg.capacity_kwh * soc.soc_now / 100 if soc is not None and soc.ok else None
            ),
            capacity_kwh=cfg.capacity_kwh, min_soc=cfg.min_soc, target_cap=cfg.target_cap,
            voltage_v=voltage, charge_efficiency=run.plan.charge_efficiency,
            max_current_a=limits.banks[0].charge_a if limits is not None else 0,
            battery_discharge_ceiling_kw=limits.banks[0].discharge_kw if limits is not None else 0,
            limits_source=run.plan.limits_source,
            effective_limits=limits,
            import_rate=schedule.prices[i],
            export_rate=(
                schedule.export_rate if event_rate is not None
                else schedule.export_prices[i] if schedule.export_prices else schedule.export_rate
            ),
            event_rate=event_rate,
            forecast_model=forecast_model_tag(run.forecast.source),
            config_generation=revision.config_generation,
        ))
    except Exception:
        log.exception("Readiness slot recording failed")


async def record_guard(
    settings: Settings, now: datetime, revision: PlanRevision | None, mode: str,
) -> None:
    """Compute Observe guard evidence alongside the guard, with separate GETs only.

    The resident setpoint is subtracted from measured import to isolate other
    load; the replay's target is the plan's requested power, not the resident
    schedule. Invalid reads remain samples, counting against later coverage.
    """
    now = now.astimezone(load_timezone(settings.timezone))
    if mode != "observe" or not settings.grid_power_entity or not _in_overnight_window(
        now.time(), parse_time(settings.charge_window_start), parse_time(settings.charge_window_end)
    ):
        return
    supply: float | None = None
    setpoint: float | None = None
    target: float | None = None
    limit_a = 0.0
    supply_voltage = 0.0
    import_w = 0.0
    eta = 0.949
    try:
        if revision is not None:
            geometry = revision.run.inputs.geometry_for(revision.run.cfg)
            plan = revision.run.plan
            if (
                geometry is not None and slot_start(now) in geometry.slot_starts
                and plan.topology is not None and plan.effective_limits is not None
            ):
                target = revision.target_w
                connection = plan.topology.connections[0]
                eta = plan.topology.banks[0].one_way_charge_efficiency
                limit_a = connection.import_limit_a
                supply_voltage = connection.supply_voltage_v
                import_w = plan.effective_limits.connections[0].import_kw * 1000
    except Exception:
        log.exception("Readiness guard target unavailable")
    try:
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            try:
                value = float((await rest.get_state(settings.grid_power_entity)).state)
                if math.isfinite(value):
                    supply = value
                value = await inverter_device(settings, rest).read_charge_rate()
                if math.isfinite(value) and value >= 0:
                    setpoint = value
            except Exception:
                pass  # Invalid evidence is recorded, not skipped or replaced by zero.
        valid = supply is not None and setpoint is not None and target is not None
        throttle: float | None = None
        breach: bool | None = None
        if supply is not None and setpoint is not None and target is not None:
            throttle = throttled_rate_w(
                supply, setpoint, target, limit_a=limit_a,
                supply_voltage_v=supply_voltage, one_way_charge_efficiency=eta,
            )
            breach = supply - setpoint + throttle / eta > import_w
        await ReadinessLedger(settings.db_path).record(GuardRecord(
            observed_at=now.astimezone(UTC), slot_start=slot_start(now),
            revision_id=revision.revision_id if revision else None, sample_valid=valid,
            measured_import_w=supply, setpoint_w=setpoint, target_w=target,
            throttle_w=throttle, breach=breach,
        ))
    except Exception:
        log.exception("Readiness guard recording failed")
