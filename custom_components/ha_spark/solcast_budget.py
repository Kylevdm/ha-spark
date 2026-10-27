"""Per-entry serialised, durable Solcast quota gate, shared by flows and setup.

A request is charged before dispatch, including failed requests. Persistence
failure fences this gate until HA restarts; it cannot silently reset the count.
The quota store contains no credentials or resource ids.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, TypeVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from .const import DOMAIN
from .solcast import Outcome, SiteResult

ResultT = TypeVar("ResultT", bound=SiteResult)


class QuotaState(BaseModel):
    """Version-one quota store, revalidated on every load."""

    model_config = ConfigDict(extra="forbid")
    utc_day: date
    calls_used: Annotated[int, Field(strict=True, ge=0, le=1000)]
    exhausted_until: AwareDatetime | None
    window_start: time | None
    first_fetch_pending: bool = Field(strict=True)
    reserved_day: date | None = None


class SolcastBudget:
    """One lock covers loading, charging, dispatch and outcome persistence."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self.store: Store[dict[str, object]] = Store(
            hass, 1, f"{DOMAIN}.solcast_quota.{entry_id}", atomic_writes=True,
        )
        self._lock = asyncio.Lock()
        self._state: QuotaState | None = None
        self._failed = False
        self.reserved_sites = 0

    @property
    def calls_used(self) -> int:
        """The last loaded count, for diagnostic display."""
        if self._state is None or self._state.utc_day < dt_util.utcnow().date():
            return 0
        return self._state.calls_used

    @property
    def state(self) -> QuotaState | None:
        """A copy for scheduling; mutations must use the locked store methods."""
        return self._state.model_copy() if self._state else None

    async def async_finish_reserved(self) -> bool:
        """Release today's reservation after its poll has been attempted."""
        async with self._lock:
            if not await self._load():
                return False
            assert self._state is not None
            self._state.reserved_day = dt_util.utcnow().date()
            return await self._save()

    async def _load(self) -> bool:
        if self._failed:
            return False
        if self._state is None:
            try:
                # Store returns None for corrupt JSON after renaming it. Spend
                # today conservatively, then recover at the next UTC midnight.
                path = Path(self.store.path)
                existed = await self._hass.async_add_executor_job(path.exists)
                raw = await self.store.async_load()
                corrupt = raw is None and existed
                if raw is not None:
                    try:
                        self._state = QuotaState.model_validate(raw)
                    except ValidationError:
                        corrupt = True
                if self._state is None:
                    self._state = QuotaState(
                        utc_day=dt_util.utcnow().date(), calls_used=1000 if corrupt else 0,
                        exhausted_until=None, window_start=None, first_fetch_pending=False,
                    )
                if corrupt:
                    return await self._save()
            except Exception:
                self._failed = True
                return False
        return True

    async def async_load(self) -> bool:
        """Load without spending quota or dispatching any request."""
        async with self._lock:
            if not await self._load():
                return False
            assert self._state is not None
            if self._state.utc_day < dt_util.utcnow().date():
                self._state.utc_day = dt_util.utcnow().date()
                self._state.calls_used = 0
                self._state.exhausted_until = None
                return await self._save()
            return True

    async def _save(self) -> bool:
        assert self._state is not None
        try:
            data = self._state.model_dump(mode="json")
            await self.store.async_save(data)
            # HA Store logs and swallows disk-write errors. Read via a separate
            # Store instance (not this instance's pending-write buffer) to prove
            # the increment landed before allowing outbound I/O.
            persisted = await Store[dict[str, object]](
                self._hass, 1, self.store.key, atomic_writes=True,
            ).async_load()
            if persisted != data:
                self._failed = True
                return False
        except Exception:
            self._failed = True
            return False
        return True

    async def async_set_first_fetch_pending(self, pending: bool) -> bool:
        """Persist the marker before a credential save (consumed by polling)."""
        async with self._lock:
            if not await self._load():
                return False
            assert self._state is not None
            self._state.first_fetch_pending = pending
            return await self._save()

    async def async_request(
        self, request: Callable[[], Awaitable[ResultT]], *, calls_limit: int,
        reserved: bool = False,
    ) -> ResultT | SiteResult:
        """Charge and save before dispatch; a broken store refuses all work."""
        async with self._lock:
            if type(calls_limit) is not int or calls_limit not in {10, 50}:
                return SiteResult(Outcome.STALE)
            if not await self._load():
                return SiteResult(Outcome.STALE)
            assert self._state is not None
            while True:
                now = dt_util.utcnow()
                if self._state.utc_day > now.date():
                    return SiteResult(Outcome.STALE)
                if self._state.utc_day < now.date():
                    self._state.utc_day = now.date()
                    self._state.calls_used = 0
                    self._state.exhausted_until = None
                if (
                    self._state.exhausted_until is not None
                    and self._state.exhausted_until > now
                ) or self._state.calls_used >= calls_limit:
                    return SiteResult(Outcome.QUOTA_EXHAUSTED)
                if (
                    not reserved and self._state.reserved_day != now.date()
                    and calls_limit - self._state.calls_used - 1 < self.reserved_sites
                ):
                    return SiteResult(Outcome.QUOTA_EXHAUSTED)
                self._state.calls_used += 1
                if not await self._save():
                    return SiteResult(Outcome.STALE)
                # Persistence can straddle midnight. Charge the new day before
                # dispatch instead of spending its first call on yesterday.
                if dt_util.utcnow().date() == self._state.utc_day:
                    break
            result = await request()
            if result.outcome == Outcome.QUOTA_EXHAUSTED:
                received_at = dt_util.utcnow()
                if self._state.utc_day < received_at.date():
                    self._state.utc_day = received_at.date()
                    self._state.calls_used = 0
                self._state.exhausted_until = datetime.combine(
                    received_at.date() + timedelta(days=1), time(), UTC,
                )
                if not await self._save():
                    return SiteResult(Outcome.STALE)
            return result


@callback
def async_get_budget(hass: HomeAssistant, entry_id: str) -> SolcastBudget:
    """Retain one gate per entry across reloads, even when setup is not loaded."""
    gates: dict[str, SolcastBudget] = hass.data.setdefault(f"{DOMAIN}_solcast_budgets", {})
    if entry_id not in gates:
        gates[entry_id] = SolcastBudget(hass, entry_id)
    gate = gates[entry_id]
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is not None:
        gate.reserved_sites = len(entry.data.get("solcast_resource_ids", []))
    return gate
