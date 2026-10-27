"""Independent Solcast polling, atomic forecast cache and quota-aware daily timers.

No startup fetch occurs unless the durable first-fetch marker is set. Failures
retain every last-good period; only fixed outcome codes may reach HA logging.
Cache keys are UTC instants so the autumn fold remains two distinct half-hours.
"""

import asyncio
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
    """Version-one cache belongs to a non-secret config-entry generation."""

    model_config = ConfigDict(extra="forbid")
    generation: str
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
        self._generation: str = entry.data.get("solcast_cache_generation", "")
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.solcast_cache.{entry.entry_id}", atomic_writes=True,
        )
        self.data = self._empty()
        self._cancel: Callable[[], None] | None = None
        self._stopped = False
        self._poll_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    def _active(self) -> bool:
        return not self._stopped

    def _empty(self) -> ForecastCache:
        return ForecastCache(
            generation=self._generation, periods=[], fetched_at=None, status="no_data",
        )

    async def async_start(self) -> None:
        """Load and validate stores without polling on a normal restart."""
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._async_start()
        finally:
            self._tasks.discard(task)

    async def _async_start(self) -> None:
        try:
            raw = await self.store.async_load()
            cache = ForecastCache.model_validate(raw)
            if cache.generation == self._generation and not self._stopped:
                self.data = cache
        except Exception:
            pass  # Store failure must never expose its exception text.
        if self._stopped:
            return
        self._prune()
        loaded = await self.budget.async_load(active=self._active)
        if self._stopped:
            return
        self.async_update_listeners()
        if self.data.status == "auth_failed":
            self.entry.async_start_reauth(self.hass)
            return
        state = self.budget.state
        if loaded and state is not None and state.first_fetch_pending:
            # Consume durably before dispatch: even a crash cannot replay this fetch.
            if await self.budget.async_set_first_fetch_pending(False, active=self._active):
                self.entry.async_create_background_task(
                    self.hass, self.async_refresh(), "Solcast first fetch",
                )
        self._schedule()

    def _prune(self) -> None:
        cutoff = dt_util.as_local(dt_util.utcnow()).date() - timedelta(days=7)
        self.data.periods = [p for p in self.data.periods
                             if dt_util.as_local(p.start).date() >= cutoff]

    async def _save(self) -> None:
        if self._stopped:
            return
        try:
            data = self.data.model_dump(mode="json")

            async def write() -> None:
                if not self._stopped:
                    await self.store.async_save(data)

            write_task = asyncio.create_task(write())
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # Drain any disk write already in flight before shutdown returns.
                await write_task
                raise
            persisted = await Store[dict[str, Any]](
                self.hass, 1, self.store.key, atomic_writes=True,
            ).async_load()
            if persisted != data:
                self.data.status = "stale"
        except Exception:
            self.data.status = "stale"

    def snapshot_forecast(self, now: datetime) -> dict[str, object]:
        """Bounded, contiguous last-good slots; never clip an oversized site sum."""
        utc_now = now.astimezone(UTC)
        start = utc_now.replace(minute=utc_now.minute // 30 * 30, second=0, microsecond=0)
        local = dt_util.as_local(now)
        end = datetime.combine(local.date() + timedelta(days=2), time(), local.tzinfo)
        periods = {p.start: p for p in self.data.periods}
        slots: list[list[object]] = []
        while start < end and len(slots) < 120:
            period = periods.get(start)
            if period is None or period.p90 > 100:
                break
            p90 = math.ceil(round(period.p90 * 1000, 6)) / 1000
            if p90 > 100:
                break
            slots.append([start.isoformat(), round(period.p10, 3), round(period.p50, 3),
                          p90])
            start += timedelta(minutes=30)
        return {
            "status": self.data.status,
            "fetched_at": self.data.fetched_at.isoformat() if self.data.fetched_at else None,
            "calls_used": self.budget.calls_used, "calls_limit": self.calls_limit,
            "slots": slots,
        }

    async def async_learn_window_start(self, value: datetime) -> None:
        """A summary adjusts timers only, independently of heartbeat liveness."""
        if not self._stopped and await self.budget.async_set_window_start(
            dt_util.as_local(value).time(), active=self._active,
        ):
            self._schedule()

    def day_totals(self, day: date) -> tuple[float, float, float] | None:
        """Day totals in kWh, or no reading when the day has no cached periods."""
        periods = [p for p in self.data.periods if dt_util.as_local(p.start).date() == day]
        if not periods:
            return None
        return (sum(p.p10 for p in periods) * 0.5, sum(p.p50 for p in periods) * 0.5,
                sum(p.p90 for p in periods) * 0.5)

    def _schedule(self, *, after_poll: bool = False) -> None:
        if self._cancel:
            self._cancel()
            self._cancel = None
        if self._stopped or self.data.status == "auth_failed":
            return
        now = dt_util.utcnow()
        local = dt_util.as_local(now)
        state = self.budget.state
        candidates: list[tuple[datetime, bool]] = []
        for offset in range(3):
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
            # A poll that overran today's reserved instant must not lose it: run it
            # now, while tonight's window has not started. A startup never polls.
            if (
                after_poll and reserved and instant <= now < instant + timedelta(minutes=20)
                and state is not None and state.reserved_day != now.date()
            ):
                future[now] = True
            if instant > now and not (
                state and state.exhausted_until and instant < state.exhausted_until
            ):
                future[instant] = bool(future.get(instant)) or reserved
        at = min(future)
        scheduled_reserved = future[at]

        async def poll() -> None:
            task = asyncio.current_task()
            assert task is not None
            self._tasks.add(task)
            try:
                if scheduled_reserved is None:
                    await self.budget.async_load(active=self._active)
                    self._prune()
                    await self._save()
                    self.async_update_listeners()
                else:
                    try:
                        data = await self._async_poll(reserved=bool(scheduled_reserved))
                    except ConfigEntryAuthFailed:
                        self.entry.async_start_reauth(self.hass)
                    else:
                        if not self._stopped:
                            self.async_set_updated_data(data)
            finally:
                self._tasks.discard(task)
                self._schedule(after_poll=True)

        @callback
        def fire(_now: datetime) -> None:
            self._cancel = None
            if not self._stopped:
                self.entry.async_create_background_task(self.hass, poll(), "Solcast poll")

        self._cancel = async_track_point_in_utc_time(self.hass, fire, at)

    async def _async_update_data(self) -> ForecastCache:
        return await self._async_poll()

    async def _async_poll(self, *, reserved: bool = False) -> ForecastCache:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            async with self._poll_lock:
                return await self._async_poll_locked(reserved=reserved)
        finally:
            self._tasks.discard(task)

    async def _async_poll_locked(self, *, reserved: bool) -> ForecastCache:
        if self._stopped or self.data.status == "auth_failed":
            return self.data
        now = dt_util.utcnow()
        local = dt_util.as_local(now)
        end = datetime.combine(local.date() + timedelta(days=2), time(), local.tzinfo)
        hours = math.ceil((end.astimezone(UTC) - now).total_seconds() / 3600)
        results: list[ForecastResult] = []
        outcome = Outcome.OK
        retries = 0
        try:
            for site in self._sites:
                while True:
                    if self._stopped:
                        return self.data
                    async def request(resource_id: str = site) -> ForecastResult:
                        return await async_fetch_forecast(
                            async_get_clientsession(self.hass), self._key, resource_id, hours=hours,
                        )
                    result = await self.budget.async_request(
                        request, calls_limit=self.calls_limit, reserved=reserved,
                        active=self._active,
                    )
                    outcome = result.outcome
                    if outcome != Outcome.BUSY or retries == 2:
                        break
                    retries += 1
                    await asyncio.sleep(retries * 15 + random.uniform(0, 15))
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
            if reserved and not self._stopped:
                await self.budget.async_finish_reserved(active=self._active)
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
        tasks = self._tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().async_shutdown()
