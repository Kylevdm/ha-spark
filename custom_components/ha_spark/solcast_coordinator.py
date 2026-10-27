"""Independent Solcast polling, atomic forecast cache and quota-aware daily timers.

No startup fetch occurs unless the durable first-fetch marker is set. Failures
retain every last-good period; only fixed outcome codes may reach HA logging.
Cache keys are UTC instants so the autumn fold remains two distinct half-hours.
"""

import asyncio
import hashlib
import logging
import math
import random
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .const import DOMAIN
from .solcast import ForecastResult, Outcome, Period, async_fetch_forecast
from .solcast_budget import async_get_budget

_LOGGER = logging.getLogger(__name__)
Status = Literal["ok", "stale", "auth_failed", "quota_exhausted", "no_data"]


class ForecastCache(BaseModel):
    """Version-one cache; identity is a digest, never a credential or site id."""

    model_config = ConfigDict(extra="forbid")
    identity: str
    periods: list[Period] = Field(max_length=2000)
    fetched_at: AwareDatetime | None
    status: Status

    @model_validator(mode="after")
    def consistent(self) -> "ForecastCache":
        """Reject duplicate periods and caches without a successful fetch time."""
        starts = [p.start for p in self.periods]
        if starts != sorted(set(starts)) or (self.periods and self.fetched_at is None):
            raise ValueError("invalid_cache")
        return self


def poll_schedule(
    day: date, *, zone: tzinfo | None, calls_limit: int, sites: int, window_start: time | None,
    sunrise: datetime | None, sunset: datetime | None,
) -> list[tuple[datetime, bool]]:
    """Today's evenly spaced daylight polls and the independent reserved poll."""
    if sunrise is None or sunset is None or sunrise >= sunset:
        start, end = datetime.combine(day, time(7), zone), datetime.combine(day, time(19), zone)
    else:
        start, end = sunrise, sunset
    count = (calls_limit - sites - 1) // sites
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    daylight = [(start + (end - start) * i / (count - 1), False) for i in range(count)]
    reserved = datetime.combine(day, window_start or time(21), zone) - timedelta(minutes=20)
    return sorted([*daylight, (reserved.astimezone(UTC), True)])


class SolcastCoordinator(DataUpdateCoordinator[ForecastCache]):
    """Own Solcast only; heartbeat liveness does not wait for or depend on it."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry[Any]) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name="Solcast")
        self.entry = entry
        self.budget = async_get_budget(hass, entry.entry_id)
        self.calls_limit: int = entry.options.get("calls_limit", 10)
        self._key: str = entry.data["solcast_api_key"]
        self._sites: tuple[str, ...] = tuple(entry.data["solcast_resource_ids"])
        self.budget.reserved_sites = len(self._sites)
        self._identity = hashlib.sha256(
            (self._key + "\0" + "\0".join(sorted(self._sites))).encode()
        ).hexdigest()
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.solcast_cache.{entry.entry_id}", atomic_writes=True,
        )
        self.data = self._empty()
        self._cancel: Callable[[], None] | None = None
        self._stopped = False
        self._reserved = False

    def _empty(self) -> ForecastCache:
        return ForecastCache(identity=self._identity, periods=[], fetched_at=None, status="no_data")

    async def async_start(self) -> None:
        """Load and validate stores without polling on a normal restart."""
        try:
            raw = await self.store.async_load()
            cache = ForecastCache.model_validate(raw)
            if cache.identity == self._identity:
                self.data = cache
        except Exception:
            pass  # Store failure must never expose its exception text.
        self._prune()
        await self._save()
        loaded = await self.budget.async_load()
        state = self.budget.state
        if loaded and state is not None and state.first_fetch_pending:
            # Consume durably before dispatch: even a crash cannot replay this fetch.
            if await self.budget.async_set_first_fetch_pending(False):
                self.entry.async_create_background_task(
                    self.hass, self.async_refresh(), "Solcast first fetch",
                )
        self._schedule()

    def _prune(self) -> None:
        cutoff = dt_util.as_local(dt_util.utcnow()).date() - timedelta(days=7)
        self.data.periods = [p for p in self.data.periods
                             if dt_util.as_local(p.start).date() >= cutoff]

    async def _save(self) -> None:
        try:
            await self.store.async_save(self.data.model_dump(mode="json"))
        except Exception:
            self.data.status = "stale"

    def day_totals(self, day: date) -> tuple[float, float, float] | None:
        """Day totals in kWh, or no reading when the day has no cached periods."""
        periods = [p for p in self.data.periods if dt_util.as_local(p.start).date() == day]
        if not periods:
            return None
        return (sum(p.p10 for p in periods) * 0.5, sum(p.p50 for p in periods) * 0.5,
                sum(p.p90 for p in periods) * 0.5)

    def _schedule(self) -> None:
        if self._cancel:
            self._cancel()
            self._cancel = None
        if self._stopped:
            return
        now = dt_util.utcnow()
        local = dt_util.as_local(now)
        state = self.budget.state
        candidates: list[tuple[datetime, bool]] = []
        for offset in range(0 if self.data.status == "auth_failed" else 3):
            day = local.date() + timedelta(days=offset)
            candidates.extend(poll_schedule(
                day, zone=local.tzinfo, calls_limit=self.calls_limit, sites=len(self._sites),
                window_start=state.window_start if state else None,
                sunrise=get_astral_event_date(self.hass, "sunrise", day),
                sunset=get_astral_event_date(self.hass, "sunset", day),
            ))
        # Day boundaries update the displayed day totals and quota without I/O
        # to Solcast. At a coincident poll instant the reserved poll takes priority.
        future: dict[datetime, bool | None] = {
            datetime.combine(local.date() + timedelta(days=1), time(), local.tzinfo)
            .astimezone(UTC): None,
            datetime.combine(now.date() + timedelta(days=1), time(), UTC): None,
        }
        for instant, reserved in candidates:
            if instant > now and not (
                state and state.exhausted_until and instant < state.exhausted_until
            ):
                future[instant] = bool(future.get(instant)) or reserved
        at = min(future)
        scheduled_reserved = future[at]

        async def poll() -> None:
            self._reserved = bool(scheduled_reserved)
            try:
                if scheduled_reserved is None:
                    await self.budget.async_load()
                    self._prune()
                    await self._save()
                    self.async_update_listeners()
                else:
                    await self.async_refresh()
            finally:
                self._reserved = False
                self._schedule()

        @callback
        def fire(_now: datetime) -> None:
            self._cancel = None
            if not self._stopped:
                self.entry.async_create_background_task(self.hass, poll(), "Solcast poll")

        self._cancel = async_track_point_in_utc_time(self.hass, fire, at)

    async def _async_update_data(self) -> ForecastCache:
        if self._stopped or self.data.status == "auth_failed":
            return self.data
        now = dt_util.utcnow()
        local = dt_util.as_local(now)
        end = datetime.combine(local.date() + timedelta(days=2), time(), local.tzinfo)
        hours = math.ceil((end.astimezone(UTC) - now).total_seconds() / 3600)
        results: list[ForecastResult] = []
        outcome = Outcome.OK
        try:
            for site in self._sites:
                for attempt in range(3):
                    if self._stopped:
                        return self.data
                    async def request(resource_id: str = site) -> ForecastResult:
                        return await async_fetch_forecast(
                            async_get_clientsession(self.hass), self._key, resource_id, hours=hours,
                        )
                    result = await self.budget.async_request(
                        request, calls_limit=self.calls_limit, reserved=self._reserved,
                    )
                    outcome = result.outcome
                    if outcome != Outcome.BUSY or attempt == 2:
                        break
                    await asyncio.sleep((attempt + 1) * 15 + random.uniform(0, 15))
                if outcome != Outcome.OK or not isinstance(result, ForecastResult):
                    break
                results.append(result)
            if self._stopped:
                return self.data
            if len(results) == len(self._sites):
                starts = {p.start for p in results[0].periods}
                if not starts or any({p.start for p in r.periods} != starts for r in results):
                    outcome = Outcome.MALFORMED
                else:
                    arrays = [{p.start: p for p in r.periods} for r in results]
                    try:
                        periods = [Period(
                            start=start,
                            p10=sum(a[start].p10 for a in arrays),
                            p50=sum(a[start].p50 for a in arrays),
                            p90=sum(a[start].p90 for a in arrays),
                        ) for start in sorted(starts)]
                    except ValidationError:
                        outcome = Outcome.MALFORMED
                    else:
                        self.data.periods = [p for p in self.data.periods if p.start < min(starts)]
                        self.data.periods.extend(periods)
                        self.data.fetched_at = dt_util.utcnow()
            self.data.status = (
                "ok" if outcome == Outcome.OK else
                "auth_failed" if outcome == Outcome.AUTH_FAILED else
                "quota_exhausted" if outcome == Outcome.QUOTA_EXHAUSTED else "stale"
            )
            self._prune()
            await self._save()
        finally:
            if self._reserved:
                await self.budget.async_finish_reserved()
        if self.data.status == "auth_failed":
            if self._cancel:
                self._cancel()
                self._cancel = None
            raise ConfigEntryAuthFailed("auth_failed") from None
        return self.data

    async def async_shutdown(self) -> None:
        """Cancel old timers; in-flight results cannot replace the unloaded cache."""
        self._stopped = True
        if self._cancel:
            self._cancel()
            self._cancel = None
        await super().async_shutdown()
