"""Durable half-hourly measurements and captured tariff evidence, independent of control.

Boundary snapshots and their closed slots are committed together. Rows can gain
fills, but boundary values and captured rate evidence never change. Forecasts
are never substituted for missing measurements.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ha_spark.config import FixedTariffConfig, Settings
from ha_spark.energy.derived_base_load import (
    ComponentSpec,
    base_load_balance,
    derive_specs_from_settings,
)
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.onboarding import ENERGY_FACTORS, POWER_FACTORS, statistic_unit
from ha_spark.energy.plausibility import plausible, plausible_kw
from ha_spark.energy.readiness_ledger import BOUNDARY_TOLERANCE, slot_start
from ha_spark.energy.readiness_measurements import (
    COMPONENTS,
    HALF_HOUR,
    RETRY_DELAY,
    SETTLE_DELAY,
    STATS_RETENTION,
    day_bounds,
    measurement_series,
)
from ha_spark.energy.session_allowance import CounterResult, read_counter
from ha_spark.energy.sources import parse_time
from ha_spark.energy.tariff import (
    HAPPY_HOUR_ALLOWANCE_KWH,
    HAPPY_HOUR_MARGIN,
    _in_overnight_window,
    _slot_overlap,
)
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.ha.statistics import list_statistic_ids, statistics_during_period
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
    """Supplier prices and trusted import-session evidence at slot close."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    import_rate: float | None = None
    export_rate: float | None = None
    revision_id: str | None = None
    import_session: ImportSession | None = None
    session_id: str | None = None
    session_start: AwareDatetime | None = None
    session_end: AwareDatetime | None = None
    session_suppressed_by: Literal["hold", "axle_export", "no_purchase_room"] | None = None
    session_remaining_kwh: float | None = Field(default=None, gt=0, le=HAPPY_HOUR_ALLOWANCE_KWH)
    exclusions: tuple[str, ...] = ()


class LedgerSlot(RateEvidence):
    """One closed slot; V2L is recorded separately from the energy balance."""

    slot_start: AwareDatetime
    components: dict[str, ComponentFlow]
    house_load_kwh: float | None = None
    base_load_kwh: float | None = None
    negative_clamped: bool = False


def _fixed_daily_rates(
    settings: Settings, start: datetime, revision_id: str,
) -> RateEvidence | None:
    """Capture the fixed supplier's flat rates when the daily model has no slot geometry."""
    try:
        tariff = FixedTariffConfig(
            rate_offpeak=settings.rate_offpeak_gbp_kwh,
            rate_peak=settings.rate_peak_gbp_kwh,
            rate_export=settings.rate_export_gbp_kwh,
            window_start=settings.charge_window_start,
            window_end=settings.charge_window_end,
        )
        local_start = start.astimezone(ZoneInfo(settings.timezone)).time()
    except (ValueError, ZoneInfoNotFoundError):
        return None
    rates = (tariff.rate_offpeak, tariff.rate_peak, tariff.rate_export)
    if not all(map(math.isfinite, rates)):
        return None
    offpeak = _in_overnight_window(
        local_start, parse_time(tariff.window_start), parse_time(tariff.window_end),
    )
    return RateEvidence(
        revision_id=revision_id,
        import_rate=tariff.rate_offpeak if offpeak else tariff.rate_peak,
        export_rate=tariff.rate_export,
    )


def capture_rates(settings: Settings, now: datetime, revision: PlanRevision | None) -> RateEvidence:
    """Read the revision held before the boundary tick replans, or fixed daily rates.

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
        if settings.tariff_provider == "fixed":
            if (rates := _fixed_daily_rates(settings, start, revision_id)) is not None:
                return rates
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
    suppressed_by: Literal["hold", "axle_export", "no_purchase_room"] | None = None
    remaining = None
    explicit = schedule.explicit_import_prices
    live_import = explicit[i] if i < len(explicit) else None
    if overlay is None:
        suppressed = schedule.suppressed_import_sessions
        reasons = schedule.import_suppression_reasons
        candidate = suppressed[i] if i < len(suppressed) else None
        reason = reasons[i] if i < len(reasons) else None
        if candidate is not None and reason in ("EV or dispatch hold", "Axle export event"):
            booking, import_session = candidate, candidate.kind
            suppressed_by = "hold" if reason == "EV or dispatch hold" else "axle_export"
        elif candidate is None and not (live_import is not None and live_import < 0):
            # A trusted booking can run out of room for a battery purchase after
            # the planning deductions and still cover measured house import.
            loads = run.inputs.load_slots
            if loads is not None and len(loads) == len(starts) == len(schedule.prices):
                for candidate in run.inputs.free_sessions:
                    if (candidate.kind != "happy_hour" or candidate.start.utcoffset() is None
                            or candidate.end.utcoffset() is None):
                        continue
                    r = run.inputs.happy_hour_remaining_kwh.get(candidate.session_id)
                    if (r is None or isinstance(r, bool) or not isinstance(r, (float, int))
                            or not math.isfinite(r) or r <= 0):
                        continue
                    shares = _slot_overlap(geometry, candidate.start, candidate.end)
                    if shares[i] <= 0:
                        continue
                    house = sum(max(0.0, load) * share for load, share in
                                zip(loads, shares, strict=True))
                    cap = min(r, HAPPY_HOUR_ALLOWANCE_KWH)
                    allowance = cap - HAPPY_HOUR_MARGIN * HAPPY_HOUR_ALLOWANCE_KWH - house
                    if math.isfinite(allowance) and allowance <= 0:
                        booking, import_session = candidate, "happy_hour"
                        suppressed_by, remaining = "no_purchase_room", cap
                        break
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
    provider = settings.tariff_provider
    # One live price interval must cover the whole closed slot: a mid-slot
    # revision's explicit price may cover only its remaining minutes.
    overlapping = [p for p in run.inputs.dynamic_prices if p.start < boundary and p.end > start]
    covered = bool(overlapping) and len({p.price for p in overlapping}) == 1 and all(
        p.start <= start and p.end >= boundary for p in overlapping
    )
    whole_slot = run.inputs.now is None or run.inputs.now <= start
    exclusions: tuple[str, ...] = ()
    if (
        provider == "dynamic" and (
            i >= len(explicit) or explicit[i] is None or not (whole_slot or covered)
        )
        # Intelligent prices come from the unit-rate feed, else from agreement rates.
        or provider == "octopus_intelligent" and not covered and run.inputs.live_rates is None
    ):
        exclusions = ("provider_rate_unavailable",)
    elif not (math.isfinite(import_rate) and math.isfinite(export_rate)):
        exclusions = ("invalid_tariff_rate",)
    elif import_session is not None and (
        (cover := booking or (session if overlay == "free_electricity" else None)) is not None
        and not (cover.start <= start and cover.end >= boundary)
    ):
        # Measured flows cannot be split at a session edge inside the slot.
        exclusions = ("partial_session_slot",)
    return RateEvidence(
        revision_id=revision_id,
        import_rate=None if exclusions else import_rate,
        export_rate=None if exclusions else export_rate,
        import_session=import_session,
        session_id=booking.session_id if booking else None,
        session_start=booking.start.astimezone(UTC) if booking else None,
        session_end=booking.end.astimezone(UTC) if booking else None,
        session_suppressed_by=suppressed_by,
        session_remaining_kwh=remaining,
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


def _configured_components(settings: Settings) -> dict[str, ComponentSpec]:
    specs = derive_specs_from_settings(settings)
    specs["v2l"] = ComponentSpec(settings.v2l_power_entity)
    return {name: spec for name, spec in specs.items() if spec.entity_id.strip()}


async def _merge_slot(
    db: aiosqlite.Connection, start: datetime, changes: dict[str, ComponentFlow],
    replace: tuple[Provenance, ...], missing: dict[str, ComponentFlow],
) -> None:
    # Read inside the caller's write transaction: a concurrently captured
    # boundary value takes precedence over either kind of fill.
    key = start.isoformat()
    async with db.execute(
        "SELECT payload FROM energy_ledger_slots WHERE slot_start = ?", (key,),
    ) as cursor:
        existing = await cursor.fetchone()
    row = LedgerSlot.model_validate_json(existing[0]) if existing else LedgerSlot(
        slot_start=start, components=missing, exclusions=("no_rate_capture",),
    )
    updates = {name: flow for name, flow in changes.items()
               if row.components[name].provenance in replace}
    if not updates:
        return
    components = row.components | updates
    house, base, clamped = _derive_loads(components)
    updated = row.model_copy(update={
        "components": components, "house_load_kwh": house, "base_load_kwh": base,
        "negative_clamped": clamped,
    })
    await db.execute(
        "INSERT INTO energy_ledger_slots VALUES (?, ?) "
        "ON CONFLICT(slot_start) DO UPDATE SET payload = excluded.payload",
        (key, updated.model_dump_json()),
    )


def _valid_interval(row: object, field: str, invert: bool) -> bool:
    # Reject each bad five-minute interval before readiness's reader sums six:
    # a negative interval must never cancel into a plausible half-hour.
    if not isinstance(row, dict):
        return False
    value = row.get(field)
    if isinstance(value, bool):
        return False
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and (-number if invert else number) >= 0


class EnergyLedger:
    """Add-on-side recorder and read-only history reader."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def record_boundary(self, settings: Settings, now: datetime, rates: RateEvidence) -> None:
        """Record the just-closed slot; call as background work, never in a control step."""
        try:
            boundary = slot_start(now)
            if now.astimezone(UTC) - boundary >= BOUNDARY_TOLERANCE:
                return
            readings: dict[str, tuple[str, bool, float]] = {}
            flows = {name: ComponentFlow(reason="not_configured") for name in COMPONENTS}
            configured = _configured_components(settings)
            if configured:
                async with HomeAssistantRest(
                    settings.ha_rest_url, settings.auth_token, timeout=settings.ha_timeout,
                ) as rest:
                    for name, spec in configured.items():
                        entity_id = spec.entity_id.strip()
                        try:
                            # The read runs after the tick's control work: accept a
                            # report from the boundary's sampling window, not later.
                            result = await read_counter(
                                rest, entity_id, now,
                                future_limit=boundary + BOUNDARY_TOLERANCE,
                            )
                        except Exception:  # noqa: BLE001 - e.g. an unconvertible report time
                            result = CounterResult(None, "unreadable")
                        if result.reading is None:
                            flows[name] = ComponentFlow(reason=result.reason)
                        else:
                            readings[name] = (entity_id, spec.invert, result.reading.kwh)
            bound_kw = plausible_kw(settings)
            await self._record(boundary, flows, readings, rates, bound_kw)
        except Exception:
            log.warning("Energy ledger recording failed")

    async def _record(
        self, boundary: datetime, flows: dict[str, ComponentFlow],
        readings: dict[str, tuple[str, bool, float]], rates: RateEvidence, bound_kw: float,
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
            distributed: dict[datetime, dict[str, ComponentFlow]] = {}
            rejected = False
            for name, (entity_id, invert, kwh) in readings.items():
                async with db.execute(
                    "SELECT boundary, entity_id, invert, kwh FROM energy_ledger_readings "
                    "WHERE component = ? AND boundary <= ? ORDER BY boundary DESC LIMIT 1",
                    (name, start_key),
                ) as cursor:
                    previous = await cursor.fetchone()
                if previous is None:
                    flows[name] = ComponentFlow(reason="no_previous_reading")
                    continue
                if (previous[1], previous[2]) != (entity_id, invert):
                    flows[name] = ComponentFlow(reason="source_changed")
                    continue
                delta = previous[3] - kwh if invert else kwh - previous[3]
                if delta < 0:
                    flows[name] = ComponentFlow(reason="counter_reset")
                    continue
                instant = datetime.fromisoformat(previous[0])
                # A recovered counter reset is one delta near the lifetime
                # reading: the reading stays the next baseline, but every slot
                # it covers becomes a rejected gap.
                if not plausible(delta, (boundary - instant).total_seconds(), bound_kw):
                    rejected = True
                    flows[name] = ComponentFlow(reason="rejected")
                    while instant < start:
                        distributed.setdefault(instant, {})[name] = ComponentFlow(
                            reason="rejected",
                        )
                        instant += HALF_HOUR
                    continue
                slots = (boundary - instant) // HALF_HOUR
                if slots == 1:
                    flows[name] = ComponentFlow(kwh=delta, provenance="boundary")
                else:
                    # Keep any captured rates for a later statistics fill;
                    # distributed provenance makes this estimate unpriceable.
                    flow = ComponentFlow(kwh=delta / slots, provenance="distributed")
                    flows[name] = flow
                    while instant < start:
                        distributed.setdefault(instant, {})[name] = flow
                        instant += HALF_HOUR
            missing = {
                name: ComponentFlow(
                    reason="not_configured" if flows[name].reason == "not_configured"
                    else "missed_boundary",
                )
                for name in COMPONENTS
            }
            if rejected:
                log.warning("Energy ledger rejected an implausible interval")
            for instant, changes in distributed.items():
                await _merge_slot(db, instant, changes, ("absent",), missing)
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
                rows = await cursor.fetchall()
        return await asyncio.to_thread(lambda: [LedgerSlot.model_validate_json(row[0])
                                                for row in rows])

    async def first_slot(self) -> datetime | None:
        """Find the earliest row, including hourly backfill, without creating the store."""
        if not await asyncio.to_thread(Path(self.db_path).exists):
            return None
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'energy_ledger_slots'",
            ) as cursor:
                if await cursor.fetchone() is None:
                    return None
            async with db.execute("SELECT MIN(slot_start) FROM energy_ledger_slots") as cursor:
                row = await cursor.fetchone()
                return datetime.fromisoformat(row[0]) if row and row[0] is not None else None

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

    async def _fill_day(
        self, settings: Settings, start: datetime, end: datetime,
        configured: dict[str, ComponentSpec],
    ) -> None:
        url, token, timeout = settings.ha_websocket_url, settings.auth_token, settings.ha_timeout
        metas = await list_statistic_ids(url, token, timeout=timeout)
        bound_kw = plausible_kw(settings)
        tz = load_timezone(settings.timezone)
        fills: dict[datetime, dict[str, ComponentFlow]] = {}
        rejected_any = False
        for name, spec in configured.items():
            entity = spec.entity_id.strip()
            meta = next((m for m in metas if isinstance(m, dict) and
                         m.get("statistic_id") == entity), None)
            if meta is None:
                continue
            fine = await statistics_during_period(url, token, entity, start,
                                                  period="5minute", timeout=timeout,
                                                  end_time=end)
            unit = statistic_unit(meta)
            if unit not in POWER_FACTORS and unit not in ENERGY_FACTORS:
                # measurement_series refuses the unit; nothing can be measured.
                continue
            power = unit in POWER_FACTORS
            field = "mean" if power else "change"
            factor = (POWER_FACTORS if power else ENERGY_FACTORS)[unit] * (
                -1 if spec.invert else 1
            )
            checked: list[dict[str, object]] = []
            rejected_slots: set[datetime] = set()
            for row in fine:
                if not _valid_interval(row, field, spec.invert) or not isinstance(row, dict):
                    continue
                try:
                    # The same conversion measurement_series applies, per row: an
                    # implausible five-minute change must never be summed into
                    # an otherwise plausible half-hour.
                    kwh = float(row[field]) * factor * (1 / 12 if power else 1)
                    tick = datetime.fromtimestamp(float(row["start"]) / 1000, UTC)
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    checked.append(row)
                    continue
                if plausible(kwh, 300.0, bound_kw):
                    checked.append(row)
                else:
                    # Only a row on one of the six ticks the slot's measurement
                    # sums can reject it; an off-tick row never contributes.
                    slot = slot_start(tick.astimezone(tz))
                    offset = tick - slot.astimezone(UTC)
                    tick_aligned = offset % timedelta(minutes=5) == timedelta(0)
                    if HALF_HOUR > offset >= timedelta(0) and tick_aligned:
                        rejected_slots.add(slot)
            try:
                series = measurement_series(checked, [], unit, spec.invert, settings.timezone)
            except ValueError:
                continue
            for instant, measured in series.items():
                if (start <= instant < end and measured.provenance == "measured"
                        and math.isfinite(measured.kwh) and measured.kwh >= 0):
                    fills.setdefault(instant, {})[name] = ComponentFlow(
                        kwh=measured.kwh, provenance="statistics_fill",
                    )
            for instant in rejected_slots:
                if start <= instant < end:
                    rejected_any = True
                    fills.setdefault(instant, {})[name] = ComponentFlow(reason="rejected")
        if rejected_any:
            log.warning("Energy ledger rejected an implausible interval")
        if not fills:
            return
        missing = {name: ComponentFlow(reason="missed_boundary" if name in configured
                                       else "not_configured") for name in COMPONENTS}
        await asyncio.to_thread(Path(self.db_path).parent.mkdir, parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.execute("BEGIN IMMEDIATE")
            partial = await _partly_filled_runs(db, start, end, fills)
            for instant, changes in fills.items():
                # A distributed run's shares sum to its counter delta: replace them
                # all or none, so a partial fill never double counts the gap.
                keep = {name: flow for name, flow in changes.items()
                        if (name, instant) in partial}
                full = {name: flow for name, flow in changes.items() if name not in keep}
                if full:
                    await _merge_slot(db, instant, full, ("absent", "distributed"), missing)
                if keep:
                    await _merge_slot(db, instant, keep, ("absent",), missing)
            await db.commit()


async def _partly_filled_runs(
    db: aiosqlite.Connection, start: datetime, end: datetime,
    fills: dict[datetime, dict[str, ComponentFlow]],
) -> set[tuple[str, datetime]]:
    """(component, slot) pairs in a multi-slot reading gap the fill does not wholly cover."""
    # A rejected half-hour is a gap, not a measurement: only a passing fill
    # counts as covering a distributed run.
    covering = {
        (name, instant)
        for instant, changes in fills.items()
        for name, flow in changes.items()
        if flow.provenance == "statistics_fill"
    }
    partial: set[tuple[str, datetime]] = set()
    for name in {name for changes in fills.values() for name in changes}:
        async with db.execute(
            "SELECT boundary FROM energy_ledger_readings WHERE component = ? AND boundary <= ? "
            "ORDER BY boundary DESC LIMIT 1", (name, start.astimezone(UTC).isoformat()),
        ) as cursor:
            first = await cursor.fetchone()
        async with db.execute(
            "SELECT boundary FROM energy_ledger_readings WHERE component = ? AND boundary > ? "
            "ORDER BY boundary", (name, start.astimezone(UTC).isoformat()),
        ) as cursor:
            previous = datetime.fromisoformat(first[0]) if first is not None else None
            async for (key,) in cursor:
                boundary = datetime.fromisoformat(key)
                if previous is not None and boundary - previous > HALF_HOUR:
                    run = []
                    at = previous
                    while at < boundary:
                        run.append(at)
                        at += HALF_HOUR
                    if not all((name, slot) in covering for slot in run):
                        partial.update((name, slot) for slot in run)
                if boundary >= end:
                    break
                previous = boundary
    return partial


# None marks a completed day; failed days carry their next attempt time.
_fill_attempts: dict[tuple[str, datetime], datetime | None] = {}


async def fill_closed_days(settings: Settings, now: datetime) -> None:
    """Reconcile settled days within recorder retention, as background work only."""
    try:
        configured = _configured_components(settings)
        if not configured:
            return
        now = now.astimezone(UTC)
        oldest = now - STATS_RETENTION
        for key in list(_fill_attempts):
            if key[1] < oldest:
                del _fill_attempts[key]
        ledger = EnergyLedger(settings.db_path)
        tz = load_timezone(settings.timezone)
        day = now.astimezone(tz).date()
        while True:
            day -= timedelta(days=1)
            start, end = day_bounds(day, settings.timezone)
            if start < oldest:
                break
            if now < end + SETTLE_DELAY:
                continue
            key = (settings.db_path, start)
            due = _fill_attempts.get(key, now)
            if due is None or now < due:
                continue
            try:
                await ledger._fill_day(settings, start, end, configured)
            except Exception:
                _fill_attempts[key] = now + RETRY_DELAY
                log.warning("Energy ledger fill failed")
            else:
                _fill_attempts[key] = None
    except Exception:
        log.warning("Energy ledger fill failed")
