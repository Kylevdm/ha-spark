"""Internal site authority persistence, owned by the daemon's write gate.

Only the daemon writer may initialise or save. Readers never create or
repair files. The separate marker binds a store UUID, not an epoch (which changes
on ordinary saves). Neither legacy proactive_mode nor device control is authority.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
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


def authority_view(
    state: AuthorityState | None, *, known: bool, save_failed: bool = False
) -> dict[str, Any]:
    """Public, secret-free authority status shared by sensors and health."""
    transition = state.transition if state is not None else None
    return {
        "mode": state.committed_mode if known and state is not None else "authority_unknown",
        "epoch": state.epoch if state is not None else None,
        "transition": transition.model_dump(mode="json") if transition else None,
        "transition_target": transition.target if transition else None,
        "transition_state": "pending" if transition else None,
        "failing_actions": list(transition.failing_actions) if transition else [],
        "holds": [hold.model_dump(mode="json") for hold in state.holds] if state else [],
        "fallback": state.fallback.model_dump(mode="json") if state else {
            "active": False, "causes": [],
        },
        "last_changed_at": state.last_changed_at.isoformat() if state else None,
        "requester_kind": state.requester_kind if state else None,
        "save_failed": save_failed,
    }


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
        self.save_failed = False
        self._pending: AuthorityState | None = None
        self._initialising = False

    @property
    def state(self) -> AuthorityState | None:
        return self._state

    @property
    def known(self) -> bool:
        return (
            self._state is not None
            and _load(self.store_path, self.marker_path).status == "known"
        )

    def start_process(self) -> None:
        """Initialise Observe or fence work from an earlier daemon process."""
        if self.known:
            assert self.state is not None
            self._pending = self.state.model_copy(update={"epoch": self.state.epoch + 1})
            self.save(self._pending)
        else:
            self.initialise()

    def retry_pending(self) -> None:
        # Only this process's own failed candidate is retried; never re-initialise.
        if self._pending is not None:
            if self._initialising:
                self._persist_initial(self._pending)
            else:
                self.save(self._pending)

    def _persist_initial(self, state: AuthorityState) -> None:
        self._pending = state
        self._initialising = True
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            _durable_write(self.store_path, state.model_dump_json())
            marker = _Marker(schema_version=1, store_id=state.store_id)
            _durable_write(self.marker_path, marker.model_dump_json())
        except (OSError, ValueError):
            self.save_failed = True
            raise
        self._state = state
        self.save_failed = False
        self._pending = None
        self._initialising = False

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
        self._persist_initial(state)
        return AuthorityLoadResult(status="known", state=state)

    def save(self, state: AuthorityState) -> None:
        # model_copy(update=...) deliberately bypasses pydantic validation, so
        # validate even a model instance before allowing anything onto disk.
        self._pending = state
        try:
            candidate = AuthorityState.model_validate_json(state.model_dump_json())
            current = _load(self.store_path, self.marker_path)
            if current.state is None or current.state.store_id != candidate.store_id:
                raise ValueError("Cannot save unknown or mismatched authority")
            _durable_write(self.store_path, candidate.model_dump_json())
        except (OSError, ValueError):
            self.save_failed = True
            raise
        self._state = candidate
        self.save_failed = False
        self._pending = None
