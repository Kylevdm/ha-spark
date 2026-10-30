"""Validated v4 snapshot facts, deliberately disconnected from actuation and planning.

Only integration-owned authority, holds and export are read. Evidence and writer
claims degrade independently; no rejected value is included in a reason code.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BeforeValidator,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from ha_spark.energy.topology import EntityId, FrozenModel, Percent, SafeId, Topology

Mode = Literal["observe", "protect", "optimise"]
Owner = Literal["addon", "pending_integration", "integration", "pending_addon"]
Code = Annotated[str, Field(strict=True, pattern=r"^[a-z_]{1,64}$")]


def _timestamp_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    return value


def _utc_instant(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise ValueError("invalid_timestamp") from None


Instant = Annotated[
    AwareDatetime, BeforeValidator(_timestamp_string), AfterValidator(_utc_instant),
]


class Transition(FrozenModel):
    """Ordered progress, not permission to execute a step."""

    target: Mode
    steps: Annotated[tuple[Code, ...], Field(max_length=32)]
    failing_actions: Annotated[tuple[Code, ...], Field(max_length=32)]


class ManualSession(FrozenModel):
    """UTC deadline, or explicitly until resumed."""

    deadline: Instant | None
    return_to: Mode
    keep_protection: bool = Field(strict=True)


class Fallback(FrozenModel):
    """Each cause recovers independently in the future authority implementation."""

    active: bool = Field(strict=True)
    causes: Annotated[
        tuple[Literal["soc_integrity", "addon_liveness"], ...], Field(max_length=2)
    ]


class Authority(FrozenModel):
    """Remote authority is information only, never an add-on write grant."""

    mode: Mode
    epoch: int = Field(strict=True, ge=1)
    store_identity: SafeId
    transition: Transition | None
    manual_session: ManualSession | None
    fallback: Fallback
    store_state: Literal["ok", "unknown", "save_failed"]


class Hold(FrozenModel):
    """Wire projection of a protection hold, not its durable ownership record."""

    cause: Literal["ev", "dispatch"]
    device_id: SafeId
    confirmed: bool = Field(strict=True)
    trusted_end: Instant | None


class Soc(FrozenModel):
    """Native observation; absent or invalid observations remain untrusted."""

    value: Percent | None
    read_at: Instant | None
    trust: Literal["trusted", "untrusted", "recovering"]
    reason: Literal[
        "ok", "implausible", "read_failed", "unavailable", "malformed", "not_finite",
        "out_of_range", "report_time_unusable", "report_time_future", "stale",
        "unchanged_too_long",
    ]

    @model_validator(mode="after")
    def trusted_observation(self) -> Self:
        if self.trust == "trusted" and (self.value is None or self.read_at is None):
            raise ValueError("invalid_soc")
        return self


class BankSoc(FrozenModel):
    """The snapshot bank id and its optional native SoC."""

    id: SafeId
    soc: Soc | None = None


class Device(FrozenModel):
    """Transport validation and capabilities, not an ownership handshake."""

    id: SafeId
    transport: Literal["legacy", "native"]
    validation: Literal["validated", "read_only"]
    reason: Literal[
        "unvalidated_identity", "identity_read_failed", "v2_tou_firmware", "caching_on",
        "gateway_not_owned", "unreachable",
    ] | None = None
    capabilities: Annotated[tuple[Code, ...], Field(max_length=32)]


class EvidenceSource(FrozenModel):
    """An entity source always carries its explicit maximum age."""

    entity_id: EntityId
    max_age_s: float = Field(strict=True, gt=0, le=86400, allow_inf_nan=False)


class Evidence(FrozenModel):
    """Native protection sources; these are not used by the legacy protection path."""

    ev_status: EvidenceSource
    battery_direction: EvidenceSource
    ev_plug: EvidenceSource | None = None
    ev_power: EvidenceSource | None = None
    live_dispatch: EvidenceSource | None = None
    adjusted_rate: EvidenceSource | None = None
    dispatch_grid_power: EvidenceSource | None = None


class Writer(FrozenModel):
    """Display-only claim for one connection's charge writer."""

    id: SafeId
    writer: bool = Field(strict=True)


class Event(FrozenModel):
    """Original event identity survives lifecycle notice retries."""

    direction: Literal["export"]
    start: Instant
    end: Instant


class Window(FrozenModel):
    """Dated export window; no register clock-face is accepted here."""

    start: Instant
    end: Instant


class ExportBound(FrozenModel):
    """Owner-confirmed rated output; this contract cannot validate the device."""

    watts: float = Field(strict=True, gt=0, le=100000, allow_inf_nan=False)
    validated: bool = Field(strict=True)
    reason: Code | None


class Supervision(FrozenModel):
    """An event-scoped owner supervision."""

    id: SafeId
    event: Event
    user_id: SafeId
    epoch: int = Field(strict=True, ge=1)
    ends_at: Instant


class ExportRecord(FrozenModel):
    """Read-only resident-state projection, not a request to arm a window."""

    event: Event
    window: Window
    store_identity: SafeId
    epoch: int = Field(strict=True, ge=1)
    supervision_id: SafeId
    state: Literal["armed", "active", "preserved", "stop_pending", "cleared", "unknown"]
    last_reconcile_at: Instant
    stop_reason: Code | None
    cleanup_verified: bool = Field(strict=True)


class StopLatch(FrozenModel):
    """Event-scoped stop, consumed by a later native export implementation."""

    event: Event
    window_end: Instant
    epoch: int = Field(strict=True, ge=1)


class ExportTransition(FrozenModel):
    """Bounded ordered notice log entry."""

    id: SafeId
    kind: Literal[
        "accepted", "armed", "started", "cleanup_verified", "aborted", "clock_hold", "tripped"
    ]
    at: Instant
    reason: Code | None


class Export(FrozenModel):
    """One inverter's export facts and unacknowledged transitions."""

    inverter_id: SafeId
    bound: ExportBound | None
    supervision: Supervision | None
    record: ExportRecord | None
    stop_latch: StopLatch | None
    transitions: Annotated[tuple[ExportTransition, ...], Field(max_length=64)]


_OWNER = TypeAdapter(Owner)
_HOLDS = TypeAdapter(Annotated[tuple[Hold, ...], Field(max_length=16)])
_DEVICES = TypeAdapter(Annotated[tuple[Device, ...], Field(max_length=8)])
_EXPORT = TypeAdapter(Annotated[tuple[Export, ...], Field(max_length=8)])
_WRITERS = TypeAdapter(Annotated[tuple[Writer, ...], Field(max_length=4)])


@dataclass(frozen=True)
class SnapshotV4:
    """Accepted inert v4 facts, kept separately from legacy intake."""

    owner: Owner
    authority: Authority | None = None
    holds: tuple[Hold, ...] = ()
    devices: tuple[Device, ...] = ()
    devices_reason: str | None = None
    banks: tuple[BankSoc, ...] = ()
    evidence: Evidence | None = None
    evidence_reason: str | None = None
    export: tuple[Export, ...] = ()
    export_reason: str | None = None
    writers: tuple[Writer, ...] = ()
    writers_reason: str | None = None


def parse_snapshot_v4(raw: dict[str, object], topology: Topology | None) -> SnapshotV4:
    """Fatal identity/authority/device errors raise; optional facts degrade locally."""
    owner = _OWNER.validate_python(raw.get("authority_owner"))
    authority = None
    holds: tuple[Hold, ...] = ()
    devices: tuple[Device, ...] = ()
    exports: tuple[Export, ...] = ()
    export_reason = None
    known = {i.id for i in topology.inverters} if topology else set()
    if owner == "integration":
        authority = Authority.model_validate(raw.get("authority"))
        holds = _HOLDS.validate_python(raw.get("holds"))
    devices_reason = None
    if raw.get("devices") is not None or owner == "integration":
        try:
            devices = _DEVICES.validate_python(raw.get("devices"))
            ids = [d.id for d in devices]
            if (len(ids) != len(set(ids)) or set(ids) != known
                    or any((d.validation == "read_only") != (d.reason is not None)
                           for d in devices)):
                raise ValueError
        except (ValidationError, ValueError, TypeError):
            if owner == "integration":
                raise ValueError("invalid_devices") from None
            # Elsewhere the spec rejects only the block.
            devices, devices_reason = (), "invalid_devices"
    if any(h.device_id not in known for h in holds):
        raise ValueError("invalid_holds")
    if owner == "integration" and raw.get("export") is not None:
        try:
            exports = _EXPORT.validate_python(raw["export"])
            ids = [e.inverter_id for e in exports]
            if len(ids) != len(set(ids)) or not set(ids) <= known:
                raise ValueError
        except (ValidationError, ValueError, TypeError):
            exports, export_reason = (), "invalid_export"
    banks: list[BankSoc] = []
    bank_raw = raw.get("banks")
    for bank in topology.banks if topology else ():
        matches = [b for b in bank_raw if isinstance(b, dict) and b.get("id") == bank.id] if (
            isinstance(bank_raw, list) and len(bank_raw) <= 6
        ) else []
        try:
            parsed = (BankSoc.model_validate(matches[0]) if len(matches) == 1
                      else BankSoc(id=bank.id))
        except ValidationError:
            parsed = BankSoc(id=bank.id)
        banks.append(parsed)
    evidence, evidence_reason = None, None
    if raw.get("evidence") is not None:
        try:
            evidence = Evidence.model_validate(raw["evidence"])
        except ValidationError:
            evidence_reason = "invalid_evidence"
    writers: tuple[Writer, ...] = ()
    writers_reason = None
    guard = raw.get("supply_guard")
    if isinstance(guard, dict) and "connections" in guard:
        try:
            connections = guard["connections"]
            if not isinstance(connections, list):
                raise ValueError
            writers = _WRITERS.validate_python([
                {"id": c.get("id"), "writer": c.get("writer")} for c in connections
                if isinstance(c, dict)
            ])
            ids = [w.id for w in writers]
            known_connections = {c.id for c in topology.connections} if topology else set()
            if (len(writers) != len(connections) or len(set(ids)) != len(ids)
                    or not set(ids) <= known_connections):
                raise ValueError
        except (ValidationError, ValueError, TypeError):
            writers, writers_reason = (), "invalid_writer"
    return SnapshotV4(owner, authority, holds, devices, devices_reason, tuple(banks), evidence,
                      evidence_reason, exports, export_reason, writers, writers_reason)
