"""Field-local v3 snapshot intake, with durable acceptance before replanning.

No values from rejected blocks are exposed. Major-load rejection clears the whole
observation set, so missing evidence can never remove the major-load reserve.
"""

from dataclasses import replace
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, field_validator

from ha_spark.config import Settings
from ha_spark.energy.topology import FrozenModel, SafeId, Topology, effective_limits
from ha_spark.energy.topology_record import (
    LoadedTopology,
    drives_writer,
    save_topology_record,
)


class TopologyBlock(FrozenModel):
    """Raw limits only; effective limits are always recomputed locally."""

    configured: bool = Field(strict=True)
    revision: int = Field(strict=True, ge=1)
    topology: Topology


class MajorLoadReading(FrozenModel):
    """A consumed power state and its source report instant, not heartbeat time."""

    id: SafeId
    draw_kw: float | None = Field(ge=0, allow_inf_nan=False, strict=True)
    reported_at: AwareDatetime | None

    @field_validator("reported_at", mode="before")
    @classmethod
    def timestamp_string(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("invalid_report_time")
        return value

    @field_validator("reported_at")
    @classmethod
    def safe_instant(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            try:
                value.timestamp()
            except (OverflowError, OSError, ValueError):
                raise ValueError("invalid_report_time") from None
        return value


class GuardConnection(FrozenModel):
    """Display-only live allowance, never a planning limit."""

    id: SafeId
    allowance_kw: float | None = Field(strict=True, ge=0, le=60, allow_inf_nan=False)


class SupplyGuardBlock(FrozenModel):
    """The complete v3 guard block, populated by the integration in a later ticket."""

    limiting: bool = Field(strict=True)
    connections: Annotated[tuple[GuardConnection, ...], Field(max_length=4)]
    restores_left: int = Field(strict=True, ge=0, le=48)
    revision: int = Field(strict=True, ge=0)


_READINGS = TypeAdapter(Annotated[list[MajorLoadReading], Field(max_length=12)])


def accept_topology(
    settings: Settings, previous: LoadedTopology, raw: object,
) -> tuple[LoadedTopology, bool]:
    """An unconfigured or rejected snapshot never replaces an accepted document."""
    if raw is None or raw == {"configured": False}:
        reason = "topology_not_configured" if previous.kind != "legacy" else None
        return replace(previous, reason=reason), False
    try:
        block = TopologyBlock.model_validate(raw)
        if not block.configured:
            return replace(previous, reason="invalid_topology"), False
        effective_limits(block.topology)
    except (ValidationError, ValueError, TypeError):
        return replace(previous, reason="invalid_topology"), False
    if previous.revision == block.revision:
        # A revision identifies an immutable document. Refuse same-revision edits.
        reason = None if previous.topology == block.topology else "invalid_topology"
        return replace(previous, reason=reason), False
    if previous.revision is not None and block.revision < previous.revision:
        return replace(previous, reason="invalid_topology"), False
    supported: tuple[int, Topology] | None = None
    if drives_writer(block.topology, settings):
        supported = (block.revision, block.topology)
    elif previous.supported_revision is not None and previous.supported_topology is not None:
        supported = (previous.supported_revision, previous.supported_topology)
    elif (previous.kind == "native" and previous.revision is not None
          and previous.topology is not None and drives_writer(previous.topology, settings)):
        supported = (previous.revision, previous.topology)
    try:
        save_topology_record(settings, block.revision, block.topology, supported)
    except OSError:
        return replace(previous, reason="topology_save_failed"), False
    return LoadedTopology(
        "native", block.revision, block.topology,
        supported_revision=supported[0] if supported is not None else None,
        supported_topology=supported[1] if supported is not None else None,
    ), True


def parse_major_loads(
    raw: object, accepted: LoadedTopology,
) -> tuple[tuple[MajorLoadReading, ...], str | None]:
    """Unknown/duplicate ids and any invalid reading reject the whole block."""
    if raw is None:
        return (), None
    try:
        readings = _READINGS.validate_python(raw)
        ids = [r.id for r in readings]
        known = {load.id for load in accepted.topology.major_loads} if accepted.topology else set()
        if len(ids) != len(set(ids)) or not set(ids) <= known:
            raise ValueError
        return tuple(readings), None
    except (ValidationError, ValueError, TypeError):
        return (), "invalid_major_loads"


def parse_supply_guard(
    raw: object, accepted: LoadedTopology,
) -> tuple[SupplyGuardBlock | None, str | None]:
    """Validate independently of topology and observations; no live allowance maths."""
    if raw is None:
        return None, None
    try:
        guard = SupplyGuardBlock.model_validate(raw)
        ids = [c.id for c in guard.connections]
        known = {c.id for c in accepted.topology.connections} if accepted.topology else set()
        if len(ids) != len(set(ids)) or not set(ids) <= known:
            raise ValueError
        return guard, None
    except (ValidationError, ValueError, TypeError):
        return None, "invalid_supply_guard"
