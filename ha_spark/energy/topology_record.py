"""Durable native topology acceptance; daemon writes, all other callers only read.

The marker is written first so interrupted first acceptance cannot restore legacy
limits. A marker's presence (even if corrupt) is sufficient to fail closed when
the bounded, revalidated record cannot be read. No authority lives here.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from ha_spark.config import Settings
from ha_spark.energy.topology import FrozenModel, Topology, effective_limits

PREVIEW_REASON = "planning across several banks or connections arrives with per-bank SoC"
UNAVAILABLE_REASON = (
    "Planning limits are unavailable: the accepted topology record cannot be read."
)
UNCONFIRMED_REASON = "every controllable inverter's limits must be confirmed"

MAX_RECORD_BYTES = 32 * 1024
_RECORD = "ha_spark_topology.json"
_MARKER = "ha_spark_topology.initialised"


class TopologyRecord(FrozenModel):
    """Versioned document, without any trusted computed limits."""

    schema_version: Literal[1]
    revision: int = Field(strict=True, ge=1)
    topology: Topology
    # The last accepted single-inverter topology; actuation keeps it while a
    # later topology is preview-only. Absent in records written before #542.
    supported_revision: int | None = Field(default=None, strict=True, ge=1)
    supported_topology: Topology | None = None


@dataclass(frozen=True)
class LoadedTopology:
    """Accepted source status, usable by daemon, CLI and agent previews alike."""

    kind: Literal["legacy", "native", "unavailable"] = "legacy"
    revision: int | None = None
    topology: Topology | None = None
    reason: str | None = None
    supported_revision: int | None = None
    supported_topology: Topology | None = None


def load_topology_record(settings: Settings) -> LoadedTopology:
    """Bound reads before parsing; never expose paths, document values or errors."""
    parent = Path(settings.db_path).parent
    try:
        with (parent / _RECORD).open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > MAX_RECORD_BYTES:
                raise ValueError
            raw = stream.read(MAX_RECORD_BYTES + 1)
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError
        record = TopologyRecord.model_validate_json(raw)
        effective_limits(record.topology)
        if record.supported_topology is not None:
            effective_limits(record.supported_topology)
        return LoadedTopology(
            "native", record.revision, record.topology,
            supported_revision=record.supported_revision,
            supported_topology=record.supported_topology,
        )
    except (OSError, ValueError, TypeError, RecursionError):
        if os.path.lexists(parent / _MARKER):
            return LoadedTopology("unavailable", reason="topology_record_unavailable")
        return LoadedTopology()


def _durable_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def save_topology_record(
    settings: Settings, revision: int, topology: Topology,
    supported: tuple[int, Topology] | None = None,
) -> None:
    """Daemon-only acceptance; failures propagate as fixed outcome codes at intake."""
    try:
        record = TopologyRecord(
            schema_version=1, revision=revision, topology=topology,
            supported_revision=supported[0] if supported is not None else None,
            supported_topology=supported[1] if supported is not None else None,
        )
        effective_limits(record.topology)
        body = record.model_dump_json().encode()
        if len(body) > MAX_RECORD_BYTES:
            raise ValueError
    except (ValidationError, ValueError, TypeError):
        raise OSError("topology_save_failed") from None
    parent = Path(settings.db_path).parent
    _durable_write(parent / _MARKER, json.dumps({"schema_version": 1}).encode())
    _durable_write(parent / _RECORD, body)


def single_inverter(topology: Topology) -> bool:
    """One connection, one bank and one inverter: the shape the live writer can follow."""
    return len(topology.connections) == len(topology.banks) == len(topology.inverters) == 1


LimitsSource = Literal["legacy", "native", "native (preview only)", "unavailable"]


@dataclass(frozen=True)
class LimitsSelection:
    """Which limits plan, which drive the legacy writer, and what is only previewed.

    ``actuation`` None means the legacy settings (or, when unavailable, nothing).
    """

    source: LimitsSource
    actuation: Topology | None = None
    preview: Topology | None = None
    reason: str | None = None


def _legacy_driver(settings: Settings) -> str | None:
    inverters = [d for d in settings.devices if d.type == "inverter"]
    return inverters[0].driver if len(inverters) == 1 else None


def _drives_writer(topology: Topology, settings: Settings) -> bool:
    return single_inverter(topology) and (
        topology.inverters[0].driver_kind == _legacy_driver(settings)
    )


def _preview(topology: Topology, settings: Settings) -> Topology | None:
    """The bank holding the legacy device's inverter, with its connection's loads."""
    driver = _legacy_driver(settings)
    inverter = next(
        (i for i in topology.inverters if i.driver_kind == driver), topology.inverters[0],
    )
    bank = next(b for b in topology.banks if b.id == inverter.bank_id)
    if bank.served_load_share_pct is not None:
        bank = bank.model_copy(update={"served_load_share_pct": 100.0})
    try:
        return Topology(
            connections=tuple(c for c in topology.connections if c.id == bank.connection_id),
            banks=(bank,),
            inverters=tuple(i for i in topology.inverters if i.bank_id == bank.id),
            v2l_chargers=tuple(v for v in topology.v2l_chargers if v.bank_id == bank.id),
            major_loads=tuple(
                m for m in topology.major_loads if m.connection_id == bank.connection_id
            ),
        )
    except (ValidationError, ValueError):
        return None


def select_limits(loaded: LoadedTopology | None, settings: Settings) -> LimitsSelection:
    """Native limits drive the writer only on an exact single-inverter match.

    Anything else is preview-only: actuation keeps the last accepted supported
    topology, or the legacy settings when there is none. An unusable record
    after native acceptance selects nothing (fail closed).
    """
    if loaded is None or loaded.kind == "legacy":
        return LimitsSelection("legacy")
    if loaded.kind == "unavailable" or loaded.topology is None:
        return LimitsSelection("unavailable", reason=UNAVAILABLE_REASON)
    if _drives_writer(loaded.topology, settings):
        return LimitsSelection("native", loaded.topology)
    kept = loaded.supported_topology
    if kept is not None and not _drives_writer(kept, settings):
        kept = None
    return LimitsSelection(
        "native (preview only)", kept, _preview(loaded.topology, settings), PREVIEW_REASON,
    )


def unconfirmed_limits(loaded: LoadedTopology) -> str | None:
    """The mode-entry refusal while any controllable inverter's limits are unconfirmed."""
    if loaded.kind == "unavailable":
        return UNAVAILABLE_REASON
    if loaded.topology is not None and any(not i.confirmed for i in loaded.topology.inverters):
        return UNCONFIRMED_REASON
    return None
