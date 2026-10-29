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

MAX_RECORD_BYTES = 32 * 1024
_RECORD = "ha_spark_topology.json"
_MARKER = "ha_spark_topology.initialised"


class TopologyRecord(FrozenModel):
    """Versioned document, without any trusted computed limits."""

    schema_version: Literal[1]
    revision: int = Field(strict=True, ge=1)
    topology: Topology


@dataclass(frozen=True)
class LoadedTopology:
    """Accepted source status, usable by daemon, CLI and agent previews alike."""

    kind: Literal["legacy", "native", "unavailable"] = "legacy"
    revision: int | None = None
    topology: Topology | None = None
    reason: str | None = None


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
        return LoadedTopology("native", record.revision, record.topology)
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


def save_topology_record(settings: Settings, revision: int, topology: Topology) -> None:
    """Daemon-only acceptance; failures propagate as fixed outcome codes at intake."""
    try:
        record = TopologyRecord(schema_version=1, revision=revision, topology=topology)
        effective_limits(record.topology)
        body = record.model_dump_json().encode()
        if len(body) > MAX_RECORD_BYTES:
            raise ValueError
    except (ValidationError, ValueError, TypeError):
        raise OSError("topology_save_failed") from None
    parent = Path(settings.db_path).parent
    _durable_write(parent / _MARKER, json.dumps({"schema_version": 1}).encode())
    _durable_write(parent / _RECORD, body)
