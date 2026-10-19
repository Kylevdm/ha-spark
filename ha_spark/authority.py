"""Internal site authority persistence; not yet consulted by any write gate.

Only the future daemon writer may initialise or save. Readers never create or
repair files. The separate marker binds a store UUID, not an epoch (which changes
on ordinary saves). Neither legacy proactive_mode nor device control is authority.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ha_spark.config import Settings

OperatingMode = Literal["observe", "protect", "optimise"]
RequesterKind = Literal["cli", "ingress", "system"]


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transition(_Record):
    target: OperatingMode
    started_at: AwareDatetime
    steps: tuple[str, ...] = ()
    failing_actions: tuple[str, ...] = ()


class HoldRecord(_Record):
    """Minimal hold intent; evidence and release details land in later tickets."""

    cause: str
    started_at: AwareDatetime


class FallbackState(_Record):
    active: bool = False
    causes: tuple[str, ...] = ()


class ReadinessNoticeState(_Record):
    notified: bool = False


class AuthorityState(_Record):
    schema_version: Literal[1]
    store_id: UUID
    committed_mode: OperatingMode
    transition: Transition | None
    epoch: int = Field(ge=1, strict=True)
    holds: tuple[HoldRecord, ...]
    fallback: FallbackState
    readiness_notice: ReadinessNoticeState
    last_changed_at: AwareDatetime
    requester_kind: RequesterKind


class _Marker(_Record):
    schema_version: Literal[1]
    store_id: UUID


class AuthorityLoadResult(_Record):
    """Unknown and uninitialised carry no mode, never an Observe default."""

    status: Literal["known", "uninitialised", "authority_unknown"]
    state: AuthorityState | None = None


def resolve_data_dir(settings: Settings) -> Path:
    """Add-on private /data; standalone uses the configured database directory.

    This is the canonical resolution for new authority files, not a migration of
    existing sidecars. It does not touch the filesystem or read legacy gates.
    """
    return Path(settings.db_path).parent if settings.is_standalone else Path("/data")


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _load(store_path: Path, marker_path: Path) -> AuthorityLoadResult:
    try:
        store_exists, marker_exists = _present(store_path), _present(marker_path)
        if not store_exists and not marker_exists:
            return AuthorityLoadResult(status="uninitialised")
        if store_exists and marker_exists:
            state = AuthorityState.model_validate_json(store_path.read_text(encoding="utf-8"))
            marker = _Marker.model_validate_json(marker_path.read_text(encoding="utf-8"))
            if state.store_id == marker.store_id:
                return AuthorityLoadResult(status="known", state=state)
    except (OSError, ValueError):
        # Includes invalid JSON/schema/encoding. Do not expose file contents.
        pass
    return AuthorityLoadResult(status="authority_unknown")


def load_authority(settings: Settings) -> AuthorityLoadResult:
    """Read only, including on a missing directory; no initialisation or repair."""
    directory = resolve_data_dir(settings)
    return _load(directory / "authority.json", directory / "authority.marker.json")


def _durable_write(path: Path, payload: str) -> None:
    """Replace atomically, then persist the directory entry; errors propagate.

    A failure after replace may already have changed disk. It is not reported as
    success and the caller must not advance its in-memory authority on that basis.
    """
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AuthorityStore:
    """Single-writer store. Save a new immutable state, then swap on success.

    Construction is read-only. Initialise explicitly, and refuse to save into an
    unknown/uninitialised store. Recovery/owner confirmation is a later ticket.
    """

    def __init__(self, settings: Settings) -> None:
        directory = resolve_data_dir(settings)
        self.store_path = directory / "authority.json"
        self.marker_path = directory / "authority.marker.json"
        self._state = _load(self.store_path, self.marker_path).state

    @property
    def state(self) -> AuthorityState | None:
        return self._state

    def initialise(self) -> AuthorityLoadResult:
        result = _load(self.store_path, self.marker_path)
        if result.status != "uninitialised":
            self._state = result.state
            return result
        state = AuthorityState(
            schema_version=1, store_id=uuid4(), committed_mode="observe", transition=None,
            epoch=1, holds=(), fallback=FallbackState(), readiness_notice=ReadinessNoticeState(),
            last_changed_at=datetime.now(UTC), requester_kind="system",
        )
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        _durable_write(self.store_path, state.model_dump_json())
        marker = _Marker(schema_version=1, store_id=state.store_id)
        _durable_write(self.marker_path, marker.model_dump_json())
        self._state = state
        return AuthorityLoadResult(status="known", state=state)

    def save(self, state: AuthorityState) -> None:
        # model_copy(update=...) deliberately bypasses pydantic validation, so
        # validate even a model instance before allowing anything onto disk.
        candidate = AuthorityState.model_validate_json(state.model_dump_json())
        current = _load(self.store_path, self.marker_path)
        if current.state is None or current.state.store_id != candidate.store_id:
            raise ValueError("Cannot save unknown or mismatched authority")
        _durable_write(self.store_path, candidate.model_dump_json())
        self._state = candidate
