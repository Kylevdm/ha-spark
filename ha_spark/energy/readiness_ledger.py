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
from ha_spark.energy.forecast import forecast_model_tag
from ha_spark.energy.plan_run import PlanRun
from ha_spark.energy.soc_integrity import SocMeasurement
from ha_spark.energy.sources import parse_time
from ha_spark.energy.supply_guard import throttled_rate_w
from ha_spark.energy.tariff import _in_overnight_window
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
    target_w: float
    config_generation: int


class SlotRecord(BaseModel):
    """Replay inputs captured together, never reconstructed from a later plan."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    slot_start: datetime
    operating_mode: str
    revision_id: str
    issued_at: datetime
    charge_window_fraction: float = Field(ge=0, le=1)
    charge_current_a: float
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
    utc = now.astimezone(UTC)
    return utc.replace(minute=utc.minute // 30 * 30, second=0, microsecond=0)


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
        intent = run.plan.charge_intent
        held = intent.hold_active(start)
        if ev_hold_active is not None:
            held = ev_hold_active or any(s <= start < e for s, e in intent.holds)
        export = intent.export
        export_kw = 0.0
        if export is not None and start in export.selected_slots:
            export_kw = export.slot_export_kw[export.selected_slots.index(start)]
        event = inputs.flexibility_event
        await ReadinessLedger(db_path).record(SlotRecord(
            slot_start=start, operating_mode=mode, revision_id=revision.revision_id,
            issued_at=revision.issued_at, charge_window_fraction=geometry.window_fracs[i],
            charge_current_a=revision.target_w / cfg.voltage_v,
            export_kw=export_kw,
            hold_state=("held" if held else "not_held") if intent.hold_trusted else "unknown",
            v2l_available=inputs.car_plugged_in,
            start_energy_kwh=(
                cfg.capacity_kwh * soc.soc_now / 100 if soc is not None and soc.ok else None
            ),
            capacity_kwh=cfg.capacity_kwh, min_soc=cfg.min_soc, target_cap=cfg.target_cap,
            voltage_v=cfg.voltage_v, charge_efficiency=cfg.charge_efficiency,
            max_current_a=cfg.max_current_a,
            battery_discharge_ceiling_kw=cfg.battery_discharge_ceiling_kw,
            import_rate=schedule.prices[i],
            export_rate=(
                schedule.export_prices[i] if schedule.export_prices else schedule.export_rate
            ),
            event_rate=event.rate_gbp_kwh if event and event.start <= start < event.end else None,
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
    if mode != "observe" or not settings.grid_power_entity or not _in_overnight_window(
        now.time(), parse_time(settings.charge_window_start), parse_time(settings.charge_window_end)
    ):
        return
    supply: float | None = None
    setpoint: float | None = None
    target: float | None = None
    try:
        if revision is not None:
            geometry = revision.run.inputs.geometry_for(revision.run.cfg)
            if geometry is not None and slot_start(now) in geometry.slot_starts:
                target = revision.target_w
        async with HomeAssistantRest(
            settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout
        ) as rest:
            try:
                value = float((await rest.get_state(settings.grid_power_entity)).state)
                if math.isfinite(value):
                    supply = value
                value = await inverter_device(settings, rest).read_charge_rate()
                if math.isfinite(value):
                    setpoint = value
            except Exception:
                pass  # Invalid evidence is recorded, not skipped or replaced by zero.
        valid = supply is not None and setpoint is not None and target is not None
        throttle: float | None = None
        breach: bool | None = None
        if supply is not None and setpoint is not None and target is not None:
            throttle = throttled_rate_w(
                supply, setpoint, target, limit_a=settings.supply_max_current_a,
                supply_voltage_v=settings.supply_voltage_v,
            )
            breach = supply - setpoint + throttle > (
                settings.supply_max_current_a * settings.supply_voltage_v
            )
        await ReadinessLedger(settings.db_path).record(GuardRecord(
            observed_at=now.astimezone(UTC), slot_start=slot_start(now),
            revision_id=revision.revision_id if revision else None, sample_valid=valid,
            measured_import_w=supply, setpoint_w=setpoint, target_w=target,
            throttle_w=throttle, breach=breach,
        ))
    except Exception:
        log.exception("Readiness guard recording failed")
