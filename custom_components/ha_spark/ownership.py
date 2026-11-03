"""Durable per-site ownership handshake. Pending and unknown never grant writes.

Topology derives a requested owner, not a write permission. Only a counted v4
reply to a pending snapshot advances the handshake. Saves are independently
reloaded, including when setup resumes a previously interrupted switch.
"""

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pydantic import BaseModel, ConfigDict

from .authority import AuthorityStore
from .repairs import ownership_switch_refused
from .topology import Topology, validate_topology
from .wire import Summary

Owner = Literal["addon", "pending_integration", "integration", "pending_addon"]


class OwnershipRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner: Owner


def derived_owner(raw: object) -> Literal["addon", "integration"]:
    """No controllable inverter, or any legacy inverter, keeps add-on authority."""
    topology = validate_topology(raw)
    if (isinstance(topology, Topology) and topology.inverters
            and all(i.transport == "native" for i in topology.inverters)):
        return "integration"
    return "addon"


class OwnershipStore:
    def __init__(
        self, hass: HomeAssistant, entry_id: str, authority: AuthorityStore,
        topology: Callable[[], object] | None = None,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.key = f"ha_spark.ownership.{entry_id}"
        self.authority = authority
        self.topology = topology
        self.last_refusal: str | None = None
        self.reconcile = False
        self.owner: Owner = "addon"
        self.verified = False
        self.loaded = False
        self._lock = asyncio.Lock()

    def _store(self) -> Store[dict[str, Any]]:
        return Store(self.hass, 1, self.key, atomic_writes=True)

    async def async_load(self) -> None:
        self.loaded = True
        try:
            # Only FileNotFoundError establishes absence, not HA's corrupt-file
            # recovery (which returns None after renaming the evidence).
            def read() -> str | None:
                try:
                    with open(self._store().path, encoding="utf-8") as stream:
                        return stream.read()
                except FileNotFoundError:
                    path = Path(self._store().path)
                    try:
                        names = os.listdir(path.parent)
                    except FileNotFoundError:
                        return None
                    if any(name.startswith(f"{path.name}.corrupt.") for name in names):
                        raise ValueError("ownership store unknown") from None
                    return None

            text = await self.hass.async_add_executor_job(read)
            if text is None and (self.topology is None
                                 or derived_owner(self.topology()) == "addon"):
                self.owner, self.verified = "addon", True
                return
            raw = await self._store().async_load()
            self.owner = OwnershipRecord.model_validate(raw).owner
            self.verified = True
            if self.topology is not None:
                target = derived_owner(self.topology())
                if target == "addon" and self.owner == "pending_integration":
                    # Crash between pending and the options commit: abort the
                    # uncommitted request, never initialise local authority.
                    await self._save("addon")
                elif target == "integration" and self.owner == "pending_addon":
                    await self._save("integration")
                elif (target == "addon" and self.owner == "integration"
                      or target == "integration" and self.owner == "addon"):
                    # A restored topology cannot grant ownership. Wait for a
                    # fresh v4 summary and the ordinary directional handshake.
                    self.reconcile = True
        except Exception:
            self.owner, self.verified = "pending_integration", False

    async def _save(self, owner: Owner) -> bool:
        # Publish the fence before the first await. A cancellation or failure
        # never exposes the previously confirmed writer as still permitted.
        self.verified = False
        self.owner = owner if owner.startswith("pending_") else self.owner
        try:
            record = OwnershipRecord(owner=owner).model_dump(mode="json")
            await self._store().async_save(record)
            if await self._store().async_load() != record:
                self.authority.publish_save_failed()
                return False
        except asyncio.CancelledError:
            self.authority.publish_save_failed()
            raise
        except Exception:
            self.authority.publish_save_failed()
            return False
        self.owner, self.verified = owner, True
        return True

    def refusal(self, target: str, summary: Summary | None) -> str | None:
        """Fixed named conditions; never return any rejected input."""
        if not self.verified:
            return "ownership_store_unknown"
        if self.owner.startswith("pending_"):
            return "ownership_switch_pending"
        if target == self.owner:
            return None
        if summary is None or summary.api_version != 4 or summary.addon_authority is None:
            return "ownership_peer_version"
        addon = summary.addon_authority
        if addon.store_state != "ok" or (self.owner == "addon" and addon.active != "active"):
            return "ownership_store_unknown"
        if addon.mode != "observe":
            return "ownership_observe_required"
        if addon.holds_present:
            return "ownership_holds_present"
        if (summary.authority.mode != "observe"
                or summary.authority.transition_target is not None):
            return "ownership_transition_present"
        if self.owner == "integration":
            state = self.authority.state
            if state is None:
                return "ownership_store_unknown"
            if state.committed_mode != "observe":
                return "ownership_observe_required"
            if state.holds:
                return "ownership_holds_present"
            if state.transition is not None:
                return "ownership_transition_present"
            if state.manual_session is not None:
                return "ownership_manual_present"
        return None

    async def async_request(
        self, target: str, summary: Summary | None,
        latest: Callable[[], Summary | None] | None = None,
    ) -> str | None:
        async with self._lock:
            # The flow may have waited on this lock; never admit using the
            # summary it captured before that wait.
            reason = self.refusal(target, latest() if latest is not None else summary)
            if reason is not None or target == self.owner:
                if reason is None:
                    self._refused(None)
                return reason
            pending: Owner = ("pending_integration" if target == "integration"
                              else "pending_addon")
            if not await self._save(pending):
                return "ownership_save_failed"
            return None

    def _refused(self, reason: str | None) -> None:
        self.last_refusal = reason
        ownership_switch_refused(self.hass, self.entry_id, reason)

    async def async_abort(self, reason: str) -> str | None:
        """Durably withdraw a pending request when its fresh admission is lost."""
        async with self._lock:
            return await self._abort(reason)

    async def _abort(self, reason: str) -> str | None:
        if self.owner not in ("pending_integration", "pending_addon"):
            return None
        owner: Owner = "addon" if self.owner == "pending_integration" else "integration"
        if not await self._save(owner):
            reason = "ownership_save_failed"
        self._refused(reason)
        return self.last_refusal

    async def async_advance(self, summary: Summary) -> str | None:
        """Called only after a fresh, session-checked heartbeat response."""
        async with self._lock:
            addon = summary.addon_authority
            if self.reconcile:
                target = "integration" if self.owner == "addon" else "addon"
                reason = self.refusal(target, summary)
                if reason is not None:
                    self._refused(reason)
                    return reason
                pending: Owner = ("pending_integration" if target == "integration"
                                  else "pending_addon")
                if await self._save(pending):
                    self.reconcile = False
                return None
            if (self.verified and not self.owner.startswith("pending_")
                    and self.topology is not None
                    and derived_owner(self.topology()) == self.owner):
                self._refused(None)
            if not self.verified:
                return None
            if self.owner.startswith("pending_"):
                reason = None
                if summary.api_version != 4 or addon is None:
                    reason = "ownership_peer_version"
                elif addon.store_state != "ok":
                    reason = "ownership_store_unknown"
                elif addon.mode != "observe":
                    reason = "ownership_observe_required"
                elif addon.holds_present:
                    reason = "ownership_holds_present"
                elif summary.authority.transition_target is not None:
                    reason = "ownership_transition_present"
                if reason is not None:
                    return await self._abort(reason)
            if addon is None:
                return None
            if self.owner == "pending_integration" and addon.active == "retired":
                state = self.authority.state
                if state is None:
                    if not await self.authority.async_initialise():
                        return None
                    state = self.authority.state
                if (state is not None and state.committed_mode == "observe" and state.epoch == 1
                        and not state.holds and state.transition is None
                        and state.manual_session is None):
                    if await self._save("integration"):
                        self._refused(None)
            elif self.owner == "pending_addon" and addon.active == "active":
                if await self.authority.async_retire():
                    if await self._save("addon"):
                        self._refused(None)
            return None
