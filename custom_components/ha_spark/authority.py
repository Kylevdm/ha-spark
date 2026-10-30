"""Per-entry two-file authority persistence, never a hardware write boundary.

Setup loads only. Initialisation belongs to the ownership handshake; recovery
requires explicit Observe confirmation. Every write is independently reloaded,
including the marker. No partially saved candidate is exposed to readers.
"""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .authority_store import AuthorityMarker, AuthorityState
from .const import DOMAIN

StoreStatus = Literal["uninitialised", "ok", "unknown", "save_failed", "retired"]


def authority_owner(
    entry: ConfigEntry[Any],
) -> Literal["addon", "pending_integration", "integration", "pending_addon"]:
    """Confirmed durable ownership, never merely the topology's requested owner."""
    ownership = getattr(getattr(entry, "runtime_data", None), "ownership", None)
    if ownership is None:
        return "addon"
    if not ownership.verified or ownership.reconcile:
        return "pending_integration"
    if ownership.owner == "integration" and ownership.authority.state is None:
        return "pending_integration"
    owner: Literal["addon", "pending_integration", "integration", "pending_addon"] = ownership.owner
    return owner


class AuthorityStore:
    """Serialise persistence and fail closed on missing, changed or invalid data."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, changed: Callable[[], None],
    ) -> None:
        self._hass = hass
        self._changed = changed
        self._lock = asyncio.Lock()
        self.store = self._store(f"{DOMAIN}.authority.{entry_id}")
        self.marker = self._store(f"{DOMAIN}.authority_marker.{entry_id}")
        self.issue_id = f"authority_unknown_{entry_id}"
        self._entry_id = entry_id
        self.status: StoreStatus = "uninitialised"
        self._state: AuthorityState | None = None

    def _store(self, key: str) -> Store[dict[str, Any]]:
        return Store(self._hass, 1, key, atomic_writes=True)

    @property
    def state(self) -> AuthorityState | None:
        """Unknown and failed saves expose no authority, even the old mode."""
        return self._state if self.status == "ok" else None

    def _publish(self, status: StoreStatus, state: AuthorityState | None = None) -> None:
        self.status = status
        if state is not None:
            self._state = state
        if status in ("unknown", "save_failed"):
            ir.async_create_issue(
                self._hass, DOMAIN, self.issue_id, is_fixable=True,
                severity=ir.IssueSeverity.ERROR, translation_key="authority_unknown",
                data={"entry_id": self._entry_id},
            )
        else:
            ir.async_delete_issue(self._hass, DOMAIN, self.issue_id)
        self._changed()

    def publish_save_failed(self) -> None:
        """Fence authority when an associated ownership commit is unverified."""
        self._publish("save_failed")

    async def _read(self) -> tuple[StoreStatus, AuthorityState | None]:
        try:
            def evidence() -> bool:
                # HA renames corrupt JSON before returning None. Keep that
                # evidence across restarts, even if both files were renamed.
                # Only "not found" means absent; any other stat or listing
                # error (permissions, I/O) raises and reads as unknown.
                paths = (Path(self.store.path), Path(self.marker.path))
                try:
                    names = os.listdir(paths[0].parent)
                except FileNotFoundError:
                    return False
                for path in paths:
                    try:
                        path.lstat()
                        return True
                    except FileNotFoundError:
                        pass
                    if any(name.startswith(f"{path.name}.corrupt.") for name in names):
                        return True
                return False

            existed = await self._hass.async_add_executor_job(evidence)
            if not existed:
                return "uninitialised", None
            raw = await self._store(self.store.key).async_load()
            marker_raw = await self._store(self.marker.key).async_load()
            # Defaults are for constructing fresh records, not for silently
            # replacing missing durable holds or other authority contents.
            if not isinstance(raw, dict) or raw.keys() != AuthorityState.model_fields.keys():
                return "unknown", None
            state = AuthorityState.model_validate(raw)
            if marker_raw == {"schema_version": 1, "store_id": str(state.store_id),
                              "retired": True}:
                return "retired", None
            marker = AuthorityMarker.model_validate(marker_raw)
            if marker.store_id == state.store_id:
                return "ok", state
        except Exception:
            pass
        return "unknown", None

    async def async_load(self) -> None:
        """Read only; a failed save remains fenced until a verified save."""
        async with self._lock:
            status, state = await self._read()
            if self.status != "save_failed":
                self._publish(status, state)

    async def _persist(self, candidate: AuthorityState, *, initial: bool) -> bool:
        try:
            candidate = AuthorityState.model_validate(candidate.model_dump(warnings=False))
            marker = AuthorityMarker(schema_version=1, store_id=candidate.store_id)

            async def write() -> None:
                await self.store.async_save(candidate.model_dump(mode="json"))
                if initial:
                    await self.marker.async_save(marker.model_dump(mode="json"))

            task = asyncio.create_task(write())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            status, loaded = await self._read()
            if status != "ok" or loaded != candidate:
                self._publish("save_failed")
                return False
        except asyncio.CancelledError:
            # Cancelled before verification: disk may hold the candidate.
            self._publish("save_failed")
            raise
        except Exception:
            self._publish("save_failed")
            return False
        self._publish("ok", candidate)
        return True

    async def async_save(self, candidate: AuthorityState) -> bool:
        """Revalidate even model_copy candidates, then independently verify disk."""
        async with self._lock:
            status, current = await self._read()
            if (status != "ok" or current is None or self._state is None
                    or current.store_id != self._state.store_id
                    or candidate.store_id != current.store_id):
                self._publish("unknown")
                return False
            return await self._persist(candidate, initial=False)

    async def _fresh_observe(self, requester: Literal["system", "repair"]) -> bool:
        return await self._persist(AuthorityState(
            store_id=uuid4(), committed_mode="observe", epoch=1,
            last_changed_at=dt_util.utcnow(), requester_kind=requester,
        ), initial=True)

    async def async_initialise(self) -> bool:
        """Explicit fresh handshake entry point; never repair damaged authority."""
        async with self._lock:
            status, _ = await self._read()
            if status not in ("uninitialised", "retired") or self.status == "save_failed":
                return False
            return await self._fresh_observe("system")

    async def async_retire(self) -> bool:
        """Invalidate the marker after an Observe-only rollback; verify before ack."""
        async with self._lock:
            status, state = await self._read()
            if status == "retired":
                return True
            if (status != "ok" or self.status == "save_failed" or state is None
                    or state.committed_mode != "observe" or state.holds
                    or state.transition is not None or state.manual_session is not None):
                return False
            try:
                await self.marker.async_save({"schema_version": 1,
                                              "store_id": str(state.store_id), "retired": True})
                loaded, _ = await self._read()
                if loaded != "retired":
                    self._publish("save_failed")
                    return False
            except asyncio.CancelledError:
                self._publish("save_failed")
                raise
            except Exception:
                self._publish("save_failed")
                return False
            self._publish("retired")
            return True

    async def async_confirm_observe(self) -> bool:
        """Recover unknown authority with a new identity and no other effects."""
        async with self._lock:
            status, _ = await self._read()
            if status == "retired":
                return False
            if status == "ok" and self.status != "save_failed":
                return False
            if status == "uninitialised" and self.status != "save_failed":
                return False
            return await self._fresh_observe("repair")
