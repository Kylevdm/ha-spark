"""Persistent per-booking grid-import counter for Happy Hours (#694).

A Happy Hour credits a booked hour's grid import up to 16 kWh. The planner must
never buy against that allowance unless it can count the energy the meter has
already delivered during the booking. This module keeps, per booked session id,
the cumulative import reading at the booking's start and the latest trusted
reading, in the add-on's SQLite data store.

The counter is deliberately fail-closed. A missing entity, an unreadable or
stale reading, a backwards reading, an implausibly large jump, a changed meter
source, or an underway booking with no trustworthy start baseline all remove
that booking's remaining allowance. A backwards or implausible reading latches
that booking as unusable for the rest of the session, across restarts: a later
plausible number cannot prove the earlier jump or reset did not happen. The
tariff overlay then leaves the hour on the ordinary tariff. Free Electricity has
no counter and is never affected.

Plausibility bound: the site's configured supply ceiling
(``supply_max_current_a * supply_voltage_v``) is the maximum AC power the grid
connection can deliver. A cumulative meter cannot rise faster than that over
the reading interval. A small fixed rounding tolerance (0.05 kWh) is added for
sensor quantisation. This is a conservative physical bound, not a learned
meter model.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

import aiosqlite

from ha_spark.config import Settings
from ha_spark.energy.models import FreeSession
from ha_spark.energy.tariff import HAPPY_HOUR_ALLOWANCE_KWH
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest

# The owner's meter freshness limit is independent of booking evidence: a
# frozen cumulative reading must not remain usable for a whole booked hour.
GRID_IMPORT_MAX_AGE = timedelta(minutes=10)
# Ended bookings are no longer needed; keep them briefly for diagnosis, then
# prune on the next due maintenance pass so the table cannot grow without bound.
ALLOWANCE_ROW_RETENTION = timedelta(hours=1)
# Cleanup is bounded: one due pass per hour, recorded in the same data store so
# a new counter instance per gather or a restart does not reset the cadence.
ALLOWANCE_CLEANUP_INTERVAL = timedelta(hours=1)
# Meter quantisation / rounding allowance on top of the physical power bound.
_JUMP_ROUNDING_TOLERANCE_KWH = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS happy_hour_allowance (
    session_id      TEXT PRIMARY KEY,
    source_identity TEXT NOT NULL,
    session_start   TEXT NOT NULL,
    session_end     TEXT NOT NULL,
    baseline_kwh    REAL NOT NULL,
    baseline_at     TEXT NOT NULL,
    latest_kwh      REAL NOT NULL,
    latest_at       TEXT NOT NULL,
    trust_reason    TEXT NOT NULL DEFAULT ''
)
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS happy_hour_allowance_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO happy_hour_allowance (
    session_id, source_identity, session_start, session_end,
    baseline_kwh, baseline_at, latest_kwh, latest_at, trust_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    source_identity = excluded.source_identity,
    session_start   = excluded.session_start,
    session_end     = excluded.session_end,
    baseline_kwh    = excluded.baseline_kwh,
    baseline_at     = excluded.baseline_at,
    latest_kwh      = excluded.latest_kwh,
    latest_at       = excluded.latest_at,
    trust_reason    = excluded.trust_reason
"""

_PRUNE = "DELETE FROM happy_hour_allowance WHERE session_end < ?"
_SELECT = (
    "SELECT session_id, source_identity, session_start, session_end, "
    "baseline_kwh, baseline_at, latest_kwh, latest_at, trust_reason "
    "FROM happy_hour_allowance WHERE session_id = ?"
)
_META_SELECT = "SELECT value FROM happy_hour_allowance_meta WHERE key = ?"
_META_UPSERT = """
INSERT INTO happy_hour_allowance_meta (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _opt_float(value: object) -> float | None:
    """Tolerant numeric coercion for values that come from outside the process."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class GridImport:
    """One trusted cumulative grid-import reading, in kWh, and its report time."""

    kwh: float
    read_at: datetime


@dataclass(frozen=True)
class GridImportResult:
    """A reading or the stable reason the counter cannot trust one."""

    reading: GridImport | None
    reason: str | None = None


@dataclass(frozen=True)
class AllowanceOutcome:
    """Remaining Happy Hour allowance per booking, plus any unusable reason.

    ``reason`` is a source-wide failure (unset, unreadable, stale or malformed
    meter). ``session_reasons`` are failures tied to one booking (no start
    baseline, backwards or implausible reading, changed meter). Both mean that
    booking buys nothing; an entry in ``remaining_kwh`` is the only thing the
    planner uses.
    """

    remaining_kwh: dict[str, float] = field(default_factory=dict)
    reason: str | None = None
    session_reasons: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _AllowanceRow:
    session_id: str
    identity: str
    start: datetime
    end: datetime
    baseline_kwh: float
    baseline_at: datetime
    latest_kwh: float
    latest_at: datetime
    trust_reason: str = ""


def allowance_source_identity(settings: Settings) -> str:
    """The non-secret source fields a persisted baseline belongs to.

    A reading from a different HA endpoint, import entity or Octopus supply is
    not comparable with this one, so its baseline can never be reused. Secrets
    (tokens and API keys) are deliberately excluded.
    """
    return "|".join((
        settings.ha_rest_url,
        settings.derive_grid_import_entity.strip(),
        settings.octopus_account_number.strip(),
        settings.octopus_mpan.strip(),
        settings.octopus_meter_serial.strip(),
    ))


async def read_grid_import(
    settings: Settings, rest: HomeAssistantRest | None, now: datetime,
) -> GridImportResult:
    """Read the cumulative import entity named by ``derive_grid_import_entity``.

    This function never raises for a bad external value: every unusable state is
    returned as a reason so the counter can remove the allowance and health can
    name it.
    """
    entity_id = settings.derive_grid_import_entity.strip()
    if not entity_id:
        return GridImportResult(None, "grid import entity is unset")
    if rest is None:
        return GridImportResult(None, "grid import entity is unreadable")
    try:
        state = await rest.get_state(entity_id)
    except Exception:  # noqa: BLE001 - a missing entity only removes the allowance
        return GridImportResult(None, "grid import entity is unreadable")
    return _grid_import_from_state(state, now)


def _grid_import_from_state(state: EntityState, now: datetime) -> GridImportResult:
    raw = str(state.state).strip()
    if raw.lower() in ("", "unknown", "unavailable"):
        return GridImportResult(None, "grid import reading is unavailable")
    value = _opt_float(raw)
    if value is None:
        return GridImportResult(None, "grid import reading is not a finite number")
    unit = str(state.attributes.get("unit_of_measurement") or "kWh").strip()
    factors = {"kWh": 1.0, "Wh": 0.001, "MWh": 1000.0}
    factor = factors.get(unit)
    if factor is None:
        return GridImportResult(None, "grid import unit is unsupported")
    kwh = value * factor
    if not math.isfinite(kwh) or kwh < 0:
        return GridImportResult(None, "grid import reading is negative or not finite")
    reported = state.last_reported
    if reported is None or reported.utcoffset() is None:
        return GridImportResult(None, "grid import report time is missing")
    reported = reported.astimezone(UTC)
    age = (now.astimezone(UTC) - reported).total_seconds()
    if age < 0:
        return GridImportResult(None, "grid import report time is in the future")
    if age > GRID_IMPORT_MAX_AGE.total_seconds():
        return GridImportResult(None, "grid import reading is stale")
    return GridImportResult(GridImport(kwh, reported))


def max_plausible_import_kw(settings: Settings) -> float:
    """The site's physical import ceiling, from the configured supply limits."""
    amps = _opt_float(settings.supply_max_current_a)
    volts = _opt_float(settings.supply_voltage_v)
    if amps is None or volts is None:
        return 0.0
    return max(0.0, amps * volts / 1000.0)


class SessionAllowanceStore:
    """Async context manager over the Happy Hour allowance table."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> SessionAllowanceStore:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        try:
            await self._db.execute(_SCHEMA)
            await self._db.execute(_META_SCHEMA)
            await self._db.commit()
        except BaseException:
            await self._db.close()
            self._db = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SessionAllowanceStore used outside 'async with'")
        return self._db

    async def load(self, session_id: str) -> _AllowanceRow | None:
        cursor = await self._conn.execute(_SELECT, (session_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _AllowanceRow(
            session_id=str(row[0]),
            identity=str(row[1]),
            start=_parse(str(row[2])),
            end=_parse(str(row[3])),
            baseline_kwh=float(row[4]),
            baseline_at=_parse(str(row[5])),
            latest_kwh=float(row[6]),
            latest_at=_parse(str(row[7])),
            trust_reason=str(row[8] or ""),
        )

    async def save(self, row: _AllowanceRow) -> None:
        await self._conn.execute(
            _UPSERT,
            (
                row.session_id, row.identity, _iso(row.start), _iso(row.end),
                row.baseline_kwh, _iso(row.baseline_at),
                row.latest_kwh, _iso(row.latest_at), row.trust_reason,
            ),
        )
        await self._conn.commit()

    async def cleanup(self, now: datetime) -> int:
        """Prune ended rows, at most once per cleanup interval."""
        cursor = await self._conn.execute(_META_SELECT, ("last_cleanup_at",))
        row = await cursor.fetchone()
        if row is not None:
            last = _parse(str(row[0]))
            if now.astimezone(UTC) - last < ALLOWANCE_CLEANUP_INTERVAL:
                return 0
        now = now.astimezone(UTC)
        cursor = await self._conn.execute(
            _PRUNE, (_iso(now - ALLOWANCE_ROW_RETENTION),),
        )
        await self._conn.execute(_META_UPSERT, ("last_cleanup_at", _iso(now)))
        await self._conn.commit()
        return cursor.rowcount


class SessionAllowanceCounter:
    """Public counter interface: persist and inspect per-booking allowances."""

    def __init__(self, db_path: str, *, identity: str, max_jump_kw: float) -> None:
        if not identity.strip():
            raise ValueError("SessionAllowanceCounter requires a source identity")
        self._db_path = db_path
        self._identity = identity
        self._max_jump_kw = max(0.0, max_jump_kw)

    async def update(
        self,
        sessions: Sequence[FreeSession],
        *,
        reading: GridImport | None,
        reason: str | None,
        now: datetime,
    ) -> AllowanceOutcome:
        """Persist the latest reading and return each booking's remaining kWh."""
        now_utc = now.astimezone(UTC)
        async with SessionAllowanceStore(self._db_path) as store:
            # Maintenance is driven by every gather, even with no bookings or a
            # meter that is currently unreadable, but never more than hourly.
            await store.cleanup(now_utc)
            happy = _happy_sessions(sessions)
            if not happy:
                return AllowanceOutcome()
            if reading is None:
                return AllowanceOutcome(
                    reason=reason or "grid import reading is unavailable",
                )
            return await self._evaluate_all(
                store, happy, reading, now_utc, persist=True,
            )

    async def inspect(
        self,
        sessions: Sequence[FreeSession],
        *,
        reading: GridImport | None,
        reason: str | None,
        now: datetime,
    ) -> AllowanceOutcome:
        """Report the same result without writing; used by health/doctor.

        A missing data store means there are no persisted baselines yet; it is
        never created by a read-only diagnostic.
        """
        happy = _happy_sessions(sessions)
        if not happy:
            return AllowanceOutcome()
        if reading is None:
            return AllowanceOutcome(reason=reason or "grid import reading is unavailable")
        if not await asyncio.to_thread(Path(self._db_path).exists):
            return await self._evaluate_all(
                None, happy, reading, now.astimezone(UTC), persist=False,
            )
        async with SessionAllowanceStore(self._db_path) as store:
            return await self._evaluate_all(
                store, happy, reading, now.astimezone(UTC), persist=False,
            )

    async def _evaluate_all(
        self,
        store: SessionAllowanceStore | None,
        happy: Sequence[FreeSession],
        reading: GridImport,
        now: datetime,
        *,
        persist: bool,
    ) -> AllowanceOutcome:
        remaining: dict[str, float] = {}
        failures: dict[str, str] = {}
        for session in happy:
            row = await store.load(session.session_id) if store is not None else None
            new_row, left, failure = _evaluate(
                session, row, reading, self._max_jump_kw, self._identity, now,
            )
            if persist and store is not None and new_row is not None:
                await store.save(new_row)
            if failure is not None or left is None:
                failures[session.session_id] = failure or "grid import is unusable"
                continue
            remaining[session.session_id] = left
        return AllowanceOutcome(remaining, session_reasons=failures)

def _happy_sessions(sessions: Sequence[FreeSession]) -> tuple[FreeSession, ...]:
    return tuple(
        session for session in sessions
        if session.kind == "happy_hour"
        and session.start.utcoffset() is not None
        and session.end.utcoffset() is not None
        and session.end > session.start
    )


def _evaluate(
    session: FreeSession,
    row: _AllowanceRow | None,
    reading: GridImport,
    max_jump_kw: float,
    identity: str,
    now: datetime,
) -> tuple[_AllowanceRow | None, float | None, str | None]:
    """Return (row to save, remaining kWh, failure reason).

    A missing start baseline is never invented from a later reading: if the
    booking is already underway and no reading at or before its start was kept,
    the booking fails closed until its next occurrence. A future booking may
    report its full allowance, but that projection is not persisted as its
    start reading. A pre-start reading only becomes the baseline while it is
    still within the meter freshness window of the start: time passing cannot
    turn an old future-hour reading into a boundary reading.
    """
    start = session.start.astimezone(UTC)
    end = session.end.astimezone(UTC)
    if row is not None and (row.start != start or row.end != end):
        # The same id with different bounds is a different booking; an old
        # baseline for other bounds can never prove this one's start.
        row = None

    if row is not None:
        if row.identity != identity:
            reason = "grid import source identity changed"
            return replace(row, trust_reason=reason), None, reason
        if row.trust_reason:
            # A reset or implausible jump has already proved this session's
            # count unusable. A later plausible number cannot undo that.
            return row, None, row.trust_reason

    if now < start:
        # A future booking may report its full allowance, but nothing is
        # persisted: a reading from before the start is not a start baseline.
        return None, HAPPY_HOUR_ALLOWANCE_KWH, None

    if row is None:
        if (reading.read_at <= start
                and start - reading.read_at <= GRID_IMPORT_MAX_AGE):
            new_row = _AllowanceRow(
                session.session_id, identity, start, end,
                reading.kwh, reading.read_at, reading.kwh, reading.read_at,
            )
            return new_row, HAPPY_HOUR_ALLOWANCE_KWH, None
        return None, None, "no trustworthy start baseline"

    if reading.read_at < row.latest_at:
        reason = "grid import report time went backwards"
        return replace(row, trust_reason=reason), None, reason
    delta = reading.kwh - row.latest_kwh
    if delta < -1e-9:
        reason = "grid import reading went backwards"
        return replace(row, trust_reason=reason), None, reason

    if reading.read_at <= start:
        # Still before this booking: roll the start baseline forward so it is
        # the last reading at or before the actual start, never an older one.
        # A reading too old to cover the boundary cannot be used for it.
        if start - reading.read_at > GRID_IMPORT_MAX_AGE:
            return None, None, "no trustworthy start baseline"
        new_row = _AllowanceRow(
            row.session_id, row.identity, row.start, row.end,
            reading.kwh, reading.read_at, reading.kwh, reading.read_at,
        )
        return new_row, HAPPY_HOUR_ALLOWANCE_KWH, None

    if delta > 0:
        elapsed_h = (reading.read_at - row.latest_at).total_seconds() / 3600.0
        allowed = max_jump_kw * elapsed_h + _JUMP_ROUNDING_TOLERANCE_KWH
        if elapsed_h <= 0 or delta > allowed:
            reason = "grid import jump is implausible"
            return replace(row, trust_reason=reason), None, reason

    new_row = replace(
        row, latest_kwh=reading.kwh, latest_at=reading.read_at, trust_reason="",
    )
    consumed = max(0.0, reading.kwh - row.baseline_kwh)
    remaining = max(0.0, HAPPY_HOUR_ALLOWANCE_KWH - consumed)
    return new_row, remaining, None
