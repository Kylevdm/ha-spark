"""Durable half-hourly measurements and captured tariff evidence, independent of control.

Boundary snapshots and their closed slots are committed together. The ledger
never revises a boundary row or substitutes forecasts for missing measurements.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ha_spark.config import Settings
from ha_spark.energy.derived_base_load import (
    ComponentSpec,
    base_load_balance,
    derive_specs_from_settings,
)
from ha_spark.energy.readiness_ledger import BOUNDARY_TOLERANCE, slot_start
from ha_spark.energy.readiness_measurements import COMPONENTS, HALF_HOUR
from ha_spark.energy.session_allowance import CounterResult, read_counter
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

if TYPE_CHECKING:
    from ha_spark.energy.readiness_ledger import PlanRevision

log = get_logger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS energy_ledger_slots (
    slot_start TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS energy_ledger_readings (
    component TEXT NOT NULL,
    boundary TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    invert INTEGER NOT NULL,
    kwh REAL NOT NULL,
    PRIMARY KEY (component, boundary)
);
"""

Provenance = Literal["boundary", "statistics_fill", "hourly_backfill", "distributed", "absent"]
ImportSession = Literal["free_electricity", "happy_hour"]


class ComponentFlow(BaseModel):
    """Measured kWh or an explicit gap for one component."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    kwh: float | None = Field(default=None, ge=0)
    provenance: Provenance = "absent"
    reason: str | None = None


class RateEvidence(BaseModel):
    """Prices and the winning import-price overlay as they were at slot close."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    import_rate: float | None = None
    export_rate: float | None = None
    revision_id: str | None = None
    import_session: ImportSession | None = None
    session_id: str | None = None
    session_start: AwareDatetime | None = None
    session_end: AwareDatetime | None = None
    exclusions: tuple[str, ...] = ()


class LedgerSlot(RateEvidence):
    """One closed slot; V2L is recorded separately from the energy balance."""

    slot_start: AwareDatetime
    components: dict[str, ComponentFlow]
    house_load_kwh: float | None = None
    base_load_kwh: float | None = None
    negative_clamped: bool = False


def capture_rates(settings: Settings, now: datetime, revision: PlanRevision | None) -> RateEvidence:
    """Read only the revision held before the boundary tick replans.

    The horizon check below is the fence: a revision stamped just after the
    boundary by a tick that began before it still prices the closed slot.
    """
    boundary = slot_start(now)
    start = boundary - HALF_HOUR
    if revision is None:
        return RateEvidence(exclusions=("no_tariff_revision",))
    run = revision.run
    geometry = run.inputs.geometry_for(run.cfg)
    revision_id = revision.revision_id
    if geometry is None:
        return RateEvidence(revision_id=revision_id, exclusions=("no_tariff_geometry",))
    starts = tuple(t.astimezone(UTC) for t in geometry.slot_starts)
    if start not in starts or (i := starts.index(start)) >= len(run.schedule.prices):
        return RateEvidence(revision_id=revision_id, exclusions=("outside_tariff_horizon",))

    schedule = run.schedule
    overlays, sessions = schedule.import_overlays, schedule.import_sessions
    overlay = overlays[i] if len(overlays) == len(schedule.prices) else None
    session = sessions[i] if len(sessions) == len(schedule.prices) else None
    booking = session if overlay == "happy_hour" and session and session.kind == overlay else None
    import_session: ImportSession | None = (
        "free_electricity" if overlay == "free_electricity" else "happy_hour" if booking else None
    )
    event = run.inputs.flexibility_event
    in_export_event = (
        event is not None and event.direction == "export"
        and event.start < boundary and event.end > start
    )
    import_rate = schedule.prices[i]
    export_rate = (
        schedule.export_prices[i]
        if not in_export_event and i < len(schedule.export_prices) else schedule.export_rate
    )
    explicit = schedule.explicit_import_prices
    provider = settings.tariff_provider
    exclusions: tuple[str, ...] = ()
    if (
        provider == "dynamic" and (i >= len(explicit) or explicit[i] is None)
        or provider == "octopus_intelligent" and run.inputs.live_rates is None
    ):
        exclusions = ("provider_rate_unavailable",)
    elif not (math.isfinite(import_rate) and math.isfinite(export_rate)):
        exclusions = ("invalid_tariff_rate",)
    return RateEvidence(
        revision_id=revision_id,
        import_rate=None if exclusions else import_rate,
        export_rate=None if exclusions else export_rate,
        import_session=import_session,
        session_id=booking.session_id if booking else None,
        session_start=booking.start.astimezone(UTC) if booking else None,
        session_end=booking.end.astimezone(UTC) if booking else None,
        exclusions=exclusions,
    )


def _derive_loads(flows: dict[str, ComponentFlow]) -> tuple[float | None, float | None, bool]:
    # V2L is its own component, never part of either load (owner ruling #608).
    if flows["grid_import"].kwh is None or any(
        flow.kwh is None and flow.reason != "not_configured"
        for name, flow in flows.items() if name != "v2l"
    ):
        return None, None, False
    values = {name: flow.kwh if flow.kwh is not None else 0.0 for name, flow in flows.items()}
    raw = base_load_balance(
        values["grid_import"], values["grid_export"], values["solar_generation"],
        values["battery_discharge"], values["battery_charge"], values["ev_charge"],
    )
    if not math.isfinite(raw):
        return None, None, False
    base = max(0.0, raw)
    house = base + values["ev_charge"]
    return (house, base, raw < 0) if math.isfinite(house) else (None, None, False)


class EnergyLedger:
    """Add-on-side boundary recorder and read-only history reader."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def record_boundary(self, settings: Settings, now: datetime, rates: RateEvidence) -> None:
        """Record the just-closed slot; call as background work, never in a control step."""
        try:
            boundary = slot_start(now)
            if now.astimezone(UTC) - boundary >= BOUNDARY_TOLERANCE:
                return
            specs = derive_specs_from_settings(settings)
            specs["v2l"] = ComponentSpec(settings.v2l_power_entity)
            readings: dict[str, tuple[str, bool, float]] = {}
            flows = {name: ComponentFlow(reason="not_configured") for name in COMPONENTS}
            configured = {name: spec for name, spec in specs.items() if spec.entity_id.strip()}
            if configured:
                async with HomeAssistantRest(
                    settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
                ) as rest:
                    for name, spec in configured.items():
                        entity_id = spec.entity_id.strip()
                        try:
                            result = await read_counter(rest, entity_id, now)
                        except Exception:  # noqa: BLE001 - e.g. an unconvertible report time
                            result = CounterResult(None, "unreadable")
                        if result.reading is None:
                            flows[name] = ComponentFlow(reason=result.reason)
                        else:
                            readings[name] = (entity_id, spec.invert, result.reading.kwh)
            await self._record(boundary, flows, readings, rates)
        except Exception:
            log.warning("Energy ledger recording failed")

    async def _record(
        self, boundary: datetime, flows: dict[str, ComponentFlow],
        readings: dict[str, tuple[str, bool, float]], rates: RateEvidence,
    ) -> None:
        start = boundary - HALF_HOUR
        start_key = start.isoformat()
        await asyncio.to_thread(Path(self.db_path).parent.mkdir, parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT 1 FROM energy_ledger_slots WHERE slot_start = ?", (start_key,),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    return
            for name, (entity_id, invert, kwh) in readings.items():
                async with db.execute(
                    "SELECT boundary, entity_id, invert, kwh FROM energy_ledger_readings "
                    "WHERE component = ? AND boundary <= ? ORDER BY boundary DESC LIMIT 1",
                    (name, start_key),
                ) as cursor:
                    previous = await cursor.fetchone()
                if previous is None:
                    flows[name] = ComponentFlow(reason="no_previous_reading")
                elif previous[0] != start_key:
                    flows[name] = ComponentFlow(reason="missed_boundary")
                elif (previous[1], previous[2]) != (entity_id, invert):
                    flows[name] = ComponentFlow(reason="source_changed")
                else:
                    delta = previous[3] - kwh if invert else kwh - previous[3]
                    flows[name] = (
                        ComponentFlow(reason="counter_reset") if delta < 0
                        else ComponentFlow(kwh=delta, provenance="boundary")
                    )
            house, base, clamped = _derive_loads(flows)
            row = LedgerSlot(
                **rates.model_dump(), slot_start=start,
                components={name: flows[name] for name in COMPONENTS},
                house_load_kwh=house, base_load_kwh=base, negative_clamped=clamped,
            )
            await db.execute(
                "INSERT INTO energy_ledger_slots VALUES (?, ?) ON CONFLICT(slot_start) DO NOTHING",
                (start_key, row.model_dump_json()),
            )
            await db.executemany(
                "INSERT INTO energy_ledger_readings VALUES (?, ?, ?, ?, ?)",
                [(name, boundary.isoformat(), *reading) for name, reading in readings.items()],
            )
            await db.commit()

    async def slots(self, start: datetime, end: datetime) -> list[LedgerSlot]:
        """Read slots in [start, end) without creating the database or its tables."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return []
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'energy_ledger_slots'",
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute(
                "SELECT payload FROM energy_ledger_slots WHERE slot_start >= ? AND slot_start < ? "
                "ORDER BY slot_start",
                (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
            ) as cursor:
                return [LedgerSlot.model_validate_json(row[0]) for row in await cursor.fetchall()]

    async def first_recorder_slot(self) -> datetime | None:
        """Find the first live or day-close row without creating the store."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return None
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'energy_ledger_slots'",
            ) as cursor:
                if await cursor.fetchone() is None:
                    return None
            cutoff = await self._first_recorder_key(db)
            return datetime.fromisoformat(cutoff) if cutoff is not None else None

    @staticmethod
    async def _first_recorder_key(db: aiosqlite.Connection) -> str | None:
        async with db.execute(
            "SELECT slot_start FROM energy_ledger_slots WHERE NOT EXISTS ("
            "SELECT 1 FROM json_each(payload, '$.exclusions') WHERE value = ?"
            ") ORDER BY slot_start LIMIT 1",
            ("hourly_backfill",),
        ) as cursor:
            row = await cursor.fetchone()
            return str(row[0]) if row is not None else None

    async def record_hourly_backfill(
        self, flows_by_slot: dict[datetime, dict[str, ComponentFlow]],
    ) -> None:
        """Insert unpriced history without changing any existing slot."""
        if not flows_by_slot:
            return

        def encode() -> list[tuple[str, str]]:
            rows = []
            for start, flows in flows_by_slot.items():
                house, base, clamped = _derive_loads(flows)
                row = LedgerSlot(
                    slot_start=start, components=flows, exclusions=("hourly_backfill",),
                    house_load_kwh=house, base_load_kwh=base, negative_clamped=clamped,
                )
                rows.append((start.astimezone(UTC).isoformat(), row.model_dump_json()))
            return rows

        rows = await asyncio.to_thread(encode)
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=rw", uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            # Day-close recording may have moved the cutoff while HA was being read.
            cutoff = await self._first_recorder_key(db)
            if cutoff is None:
                return
            await db.executemany(
                "INSERT INTO energy_ledger_slots VALUES (?, ?) ON CONFLICT(slot_start) DO NOTHING",
                [(key, payload) for key, payload in rows if key < cutoff],
            )
            await db.commit()
