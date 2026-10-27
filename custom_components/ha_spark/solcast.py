"""HA-free, bounded Solcast site discovery with fixed, secret-free outcomes.

Only the fixed origin receives the bearer header. Raw HTTP data and validation
exceptions never leave this module; resource ids are private entry data.
"""

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal

import aiohttp
from pydantic import BaseModel, Field, ValidationError, field_validator

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

    resource_id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9-]{1,64}$")]


class SiteList(BaseModel):
    """Validate the complete list before allowing any id into entry data."""

    sites: list[Site] = Field(max_length=1000)

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


async def async_list_sites(session: aiohttp.ClientSession, key: str) -> SiteResult:
    """Discover sites; callers must dispatch through the per-entry budget gate."""
    try:
        async with asyncio.timeout(20), session.get(
            f"{ORIGIN}/rooftop_sites?format=json",
            headers={"Authorization": f"Bearer {key}"}, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as response:
            if response.status in {401, 403}:
                return SiteResult(Outcome.AUTH_FAILED)
            if 500 <= response.status < 600:
                return SiteResult(Outcome.SERVER_ERROR)
            if response.status not in {200, 429}:
                return SiteResult(Outcome.MALFORMED)
            cap = 256 * 1024
            read_limit = 4096 if response.status == 429 else cap + 1
            raw = b""
            stream = response.content
            while len(raw) < read_limit and (chunk := await stream.read(read_limit - len(raw))):
                raw += chunk
            if response.status == 200 and len(raw) > cap:
                return SiteResult(Outcome.MALFORMED)
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
                return SiteResult(Outcome.QUOTA_EXHAUSTED if exhausted else Outcome.BUSY)
            sites = SiteList.model_validate_json(raw).sites
            if not sites:
                return SiteResult(Outcome.MALFORMED, flow_error="no_sites")
            if len(sites) > 2:
                return SiteResult(Outcome.MALFORMED, flow_error="too_many_sites")
            return SiteResult(Outcome.OK, tuple(site.resource_id for site in sites))
    except (ValidationError, ValueError, UnicodeError):
        return SiteResult(Outcome.MALFORMED)
    except Exception:  # No exception text or chained exception may reach HA.
        return SiteResult(Outcome.UNREACHABLE)
