"""Record accepted Axle lifecycle facts, without supervising or issuing exports.

The store freezes acceptance evidence. Delivery and earnings are read-time
statistics so later ledger fills can complete an event without repricing it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Self

import aiosqlite
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ha_spark.energy.export_store import ExportEventStore
from ha_spark.logging import get_logger

if TYPE_CHECKING:
    from ha_spark.api.snapshot_v4 import SnapshotV4
    from ha_spark.config import Settings
    from ha_spark.energy.readiness_ledger import PlanRevision

log = get_logger(__name__)
StopReason = Literal["aborted", "tripped", "unverified"]
Identity = tuple[str, datetime, datetime]
_SCHEMA = """
CREATE TABLE IF NOT EXISTS axle_events (
    event_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    committed_kwh REAL,
    rate_gbp_kwh REAL NOT NULL,
    cancelled_at TEXT,
    stop_reason TEXT
)
"""


def _key(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="microseconds")


def _event_id(identity: Identity) -> str:
    direction, start, end = identity
    return f"{direction}|{start.astimezone(UTC).isoformat()}|{end.astimezone(UTC).isoformat()}"


class AxleEvent(BaseModel):
    """An accepted window and its immutable commitment and rate evidence."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    direction: Literal["export"] = "export"
    start: AwareDatetime
    end: AwareDatetime
    accepted_at: AwareDatetime
    committed_kwh: float | None = Field(default=None, ge=0)
    rate_gbp_kwh: float = Field(ge=0)
    cancelled_at: AwareDatetime | None = None
    stop_reason: StopReason | None = None

    @model_validator(mode="after")
    def _valid_window(self) -> Self:
        if self.end.astimezone(UTC) <= self.start.astimezone(UTC):
            raise ValueError("Invalid Axle event window")
        return self


class _Stop(NamedTuple):
    event_id: str
    reason: StopReason


class _Read(NamedTuple):
    at: datetime
    event_id: str | None


_Fact = AxleEvent | _Stop | _Read


class AxleEventStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def _record(self, facts: tuple[_Fact, ...], recent_reads: tuple[_Read, ...]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(_SCHEMA)
            for event in (fact for fact in facts if isinstance(fact, AxleEvent)):
                event_id = _event_id((event.direction, event.start, event.end))
                accepted, end = _key(event.accepted_at), _key(event.end)
                # A native acceptance can arrive after the subsequent supplier read.
                cancelled = min((
                    stamp for read in recent_reads
                    if read.event_id != event_id and accepted < (stamp := _key(read.at)) < end
                ), default=None)
                await db.execute(
                    "INSERT INTO axle_events (event_id, direction, start, end, accepted_at, "
                    "committed_kwh, rate_gbp_kwh, cancelled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    (event_id, event.direction, _key(event.start), end, accepted,
                     event.committed_kwh, event.rate_gbp_kwh, cancelled),
                )
            for fact in facts:
                if isinstance(fact, _Stop):
                    await db.execute(
                        "UPDATE axle_events SET stop_reason = ? "
                        "WHERE event_id = ? AND stop_reason IS NULL", (fact.reason, fact.event_id),
                    )
                elif isinstance(fact, _Read):
                    stamp = _key(fact.at)
                    await db.execute(
                        "UPDATE axle_events SET cancelled_at = ? WHERE cancelled_at IS NULL "
                        "AND accepted_at < ? AND end > ? AND event_id IS NOT ?",
                        (stamp, stamp, stamp, fact.event_id),
                    )
            await db.commit()

    async def events(self, start: datetime, end: datetime) -> list[AxleEvent]:
        """Read windows overlapping [start, end), without creating a store."""
        path = await asyncio.to_thread(Path(self.db_path).resolve)
        if _key(end) <= _key(start) or not await asyncio.to_thread(path.exists):
            return []
        async with aiosqlite.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", ("table", "axle_events"),
            ) as cursor:
                if await cursor.fetchone() is None:
                    return []
            async with db.execute(
                "SELECT direction, start, end, accepted_at, committed_kwh, rate_gbp_kwh, "
                "cancelled_at, stop_reason FROM axle_events WHERE start < ? AND end > ? "
                "ORDER BY start, end", (_key(end), _key(start)),
            ) as cursor:
                return [AxleEvent.model_validate(dict(row)) for row in await cursor.fetchall()]


def _accept(
    identity: Identity, at: datetime, rate: float, revision: PlanRevision | None,
) -> AxleEvent:
    intent = revision.run.plan.charge_intent if revision is not None else None
    export = intent.export if intent is not None else None
    direction, start, end = identity
    committed = None
    if export is not None and _event_id(export.event_identity) == _event_id(identity):
        committed = sum(kw * 0.5 for _, kw in zip(
            export.selected_slots, export.slot_export_kw, strict=True,
        ))
    return AxleEvent.model_validate({
        "direction": direction, "start": start, "end": end, "accepted_at": at,
        "committed_kwh": committed, "rate_gbp_kwh": rate,
    })


def _load_verified_export(db_path: str) -> tuple[str, datetime] | None:
    # The async-shaped sidecar reader performs synchronous file I/O.
    return asyncio.run(ExportEventStore(db_path).load())


class AxleEventRecorder:
    """Hold lifecycle facts across snapshot acks and transient SQLite failures."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], _Fact] = {}
        self._recent_reads: deque[_Read] = deque(maxlen=64)

    def _keep(self, kind: str, key: str, fact: _Fact) -> None:
        self._pending.setdefault((kind, key), fact)
        if len(self._pending) > 1024:  # Two full native snapshots' worth of retry evidence.
            self._pending.pop(next(iter(self._pending)))

    def capture_native(
        self, snapshot: SnapshotV4 | None, settings: Settings, revision: PlanRevision | None,
    ) -> bool:
        """Copy inert lifecycle facts before the existing notice pass can ack them."""
        if snapshot is None or snapshot.owner != "integration":
            return False
        rows = sorted((row for entry in snapshot.export for row in entry.transitions),
                      key=lambda row: row.at)
        for row in rows:
            if row.event.direction != "export":
                continue
            try:
                identity = ("export", row.event.start, row.event.end)
                event_id = _event_id(identity)
                if row.kind == "accepted":
                    self._keep("accepted", event_id, _accept(
                        identity, row.at, settings.axle_event_rate_gbp_kwh, revision,
                    ))
                elif row.kind in ("aborted", "tripped", "unverified"):
                    self._keep("stop", event_id, _Stop(event_id, row.kind))
            except Exception:
                log.warning("Axle event recording failed")
        return True

    def capture_read(self, settings: Settings, revision: PlanRevision | None) -> None:
        """Keep only actual, trusted Axle reads, never a disabled source's None."""
        if settings.export_event_provider != "axle" or revision is None:
            return
        try:
            inputs = revision.run.inputs
            if inputs.flexibility_event_trusted:
                event = inputs.flexibility_event
                read = _Read(
                    revision.issued_at, _event_id(event.identity) if event is not None else None,
                )
                self._recent_reads.append(read)
                self._keep("read", revision.revision_id, read)
        except Exception:
            log.warning("Axle event recording failed")

    async def record_tick(
        self, settings: Settings, now: datetime, revision: PlanRevision | None, integration: bool,
    ) -> None:
        """Read the legacy sidecar off-loop and persist queued facts in one transaction."""
        try:
            if not integration:
                await self._capture_legacy(settings, now, revision)
            batch = tuple(self._pending.items())
            if batch:
                await AxleEventStore(settings.db_path)._record(
                    tuple(fact for _, fact in batch), tuple(self._recent_reads),
                )
                for key, fact in batch:
                    if self._pending.get(key) is fact:
                        self._pending.pop(key)
        except Exception:
            log.warning("Axle event recording failed")

    async def _capture_legacy(
        self, settings: Settings, now: datetime, revision: PlanRevision | None,
    ) -> None:
        try:
            verified = await asyncio.to_thread(_load_verified_export, settings.db_path)
            if verified is None:
                return
            raw_id, _ = verified
            direction, raw_start, raw_end = raw_id.split("|", 2)
            identity = (
                direction, datetime.fromisoformat(raw_start), datetime.fromisoformat(raw_end),
            )
            accepted_at = max(now, revision.issued_at) if revision is not None else now
            self._keep("accepted", _event_id(identity), _accept(
                identity, accepted_at, settings.axle_event_rate_gbp_kwh, revision,
            ))
        except Exception:
            log.warning("Axle event recording failed")
