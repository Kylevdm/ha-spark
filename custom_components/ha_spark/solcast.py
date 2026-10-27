"""HA-free, bounded Solcast HTTP client with fixed, secret-free outcomes.

Only the fixed origin receives the bearer header. Raw HTTP data and validation
exceptions never leave this module; resource ids are private entry data.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import quote

import aiohttp
from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ORIGIN = "https://api.solcast.com.au"


class Outcome(StrEnum):
    """Allowlisted request outcomes, plus a fail-closed budget outcome."""

    OK = "ok"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BUSY = "busy"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"
    MALFORMED = "malformed"
    STALE = "stale"


class Site(BaseModel):
    """Only the resource id is consumed; other Solcast metadata is ignored."""

    resource_id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9-]{1,64}$", repr=False)]


class SiteList(BaseModel):
    """Validate the complete list before allowing any id into entry data."""

    sites: list[Site] = Field(repr=False)

    @field_validator("sites")
    @classmethod
    def unique_sites(cls, sites: list[Site]) -> list[Site]:
        """Duplicate sites are malformed, not a second array."""
        if len({site.resource_id for site in sites}) != len(sites):
            raise ValueError("duplicate_sites")
        return sites


@dataclass(frozen=True)
class SiteResult:
    """No response body, URL or credential is retained in a result."""

    outcome: Outcome
    resource_ids: tuple[str, ...] = field(default=(), repr=False)
    flow_error: Literal["no_sites", "too_many_sites"] | None = None


Power = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False, strict=True)]


class Period(BaseModel):
    """A cached mean-power triple keyed by its real UTC start instant."""

    start: AwareDatetime
    p10: Annotated[float, Field(ge=0, le=200, allow_inf_nan=False, strict=True)]
    p50: Annotated[float, Field(ge=0, le=200, allow_inf_nan=False, strict=True)]
    p90: Annotated[float, Field(ge=0, le=200, allow_inf_nan=False, strict=True)]

    @field_validator("start")
    @classmethod
    def aligned(cls, value: datetime) -> datetime:
        """Store UTC instants, never ambiguous local wall-clock keys."""
        value = value.astimezone(UTC)
        if value.minute not in {0, 30} or value.second or value.microsecond:
            raise ValueError("unaligned")
        return value

    @model_validator(mode="after")
    def ordered(self) -> "Period":
        """Percentiles must be ordered before entering the cache."""
        if not self.p10 <= self.p50 <= self.p90:
            raise ValueError("unordered")
        return self


class ForecastPeriod(BaseModel):
    """Validate Solcast's period-end convention before conversion."""

    period_end: AwareDatetime
    period: Literal["PT30M"]
    pv_estimate10: Power
    pv_estimate: Power
    pv_estimate90: Power


class Forecast(BaseModel):
    """Bound the number of external periods before conversion."""

    forecasts: list[ForecastPeriod] = Field(min_length=1, max_length=1000)


@dataclass(frozen=True)
class ForecastResult(SiteResult):
    """A complete validated site's periods, or an empty fixed-code failure."""

    periods: tuple[Period, ...] = ()


async def _async_get(
    session: aiohttp.ClientSession, key: str, path: str,
) -> tuple[Outcome, bytes]:
    """Shared bounded transport; callers must use the per-entry budget gate."""
    try:
        async with asyncio.timeout(20), session.get(
            f"{ORIGIN}{path}",
            headers={"Authorization": f"Bearer {key}"}, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status in {401, 403}:
                return Outcome.AUTH_FAILED, b""
            if 500 <= response.status < 600:
                return Outcome.SERVER_ERROR, b""
            if response.status not in {200, 429}:
                return Outcome.MALFORMED, b""
            cap = 256 * 1024
            read_limit = 4096 if response.status == 429 else cap + 1
            raw = b""
            stream = response.content
            while len(raw) < read_limit and (chunk := await stream.read(read_limit - len(raw))):
                raw += chunk
            if response.status == 200 and len(raw) > cap:
                return Outcome.MALFORMED, b""
            if response.status == 429:
                try:
                    body = json.loads(raw)
                    exhausted = (
                        isinstance(body, dict)
                        and isinstance(body.get("response_status"), dict)
                        and body["response_status"].get("error_code") == "TooManyRequests"
                    )
                except (ValueError, UnicodeError):
                    exhausted = False
                return (Outcome.QUOTA_EXHAUSTED if exhausted else Outcome.BUSY), b""
            return Outcome.OK, raw
    except Exception:  # Never propagate URLs, response bodies or exception text to HA.
        return Outcome.UNREACHABLE, b""


async def async_list_sites(session: aiohttp.ClientSession, key: str) -> SiteResult:
    """Discover sites through the same transport as polling."""
    outcome, raw = await _async_get(session, key, "/rooftop_sites?format=json")
    if outcome != Outcome.OK:
        return SiteResult(outcome)
    try:
        sites = SiteList.model_validate_json(raw).sites
    except (ValidationError, ValueError, UnicodeError):
        return SiteResult(Outcome.MALFORMED)
    if not sites:
        return SiteResult(Outcome.MALFORMED, flow_error="no_sites")
    if len(sites) > 2:
        return SiteResult(Outcome.MALFORMED, flow_error="too_many_sites")
    return SiteResult(Outcome.OK, tuple(site.resource_id for site in sites))


async def async_fetch_forecast(
    session: aiohttp.ClientSession, key: str, resource_id: str, *, hours: int,
) -> ForecastResult:
    """Fetch one site's undampened forecast, with its id confined to the path."""
    path = f"/rooftop_sites/{quote(resource_id, safe='')}/forecasts?format=json&hours={hours}"
    outcome, raw = await _async_get(session, key, path)
    if outcome != Outcome.OK:
        return ForecastResult(outcome)
    try:
        forecast = Forecast.model_validate_json(raw)
        periods = tuple(Period(
            start=p.period_end.astimezone(UTC) - timedelta(minutes=30),
            p10=p.pv_estimate10, p50=p.pv_estimate, p90=p.pv_estimate90,
        ) for p in forecast.forecasts)
        if len({p.start for p in periods}) != len(periods):
            return ForecastResult(Outcome.MALFORMED)
        return ForecastResult(Outcome.OK, periods=tuple(sorted(periods, key=lambda p: p.start)))
    except (ValidationError, ValueError, UnicodeError, OverflowError):
        return ForecastResult(Outcome.MALFORMED)
