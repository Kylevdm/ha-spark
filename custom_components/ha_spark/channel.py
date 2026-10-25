"""Bounded heartbeat HTTP client; only fixed outcome codes cross the boundary.

No request, response, credential or exception text is logged or retained. Redirects
are disabled so the discovery endpoint cannot forward the bearer credential.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum

import aiohttp

from .wire import SUPPORTED_API_VERSIONS, Summary, parse_summary

_LOGGER = logging.getLogger(__name__)


class Outcome(StrEnum):
    """Allowlisted channel and liveness outcomes."""

    COUNTED = "counted"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    MALFORMED = "malformed"
    STALE_TICK = "stale_tick"
    FUTURE_TICK = "future_tick"
    NULL_TICK = "null_tick"
    UNAUTHORIZED = "unauthorized"
    SESSION_MISMATCH = "session_mismatch"
    VERSION_REFUSED = "version_refused"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class PollResult:
    """A validated envelope or a fixed failure code, never raw HTTP data."""

    outcome: Outcome
    summary: Summary | None = None


async def async_poll(
    session: aiohttp.ClientSession, *, url: str, token: str, snapshot: dict[str, object],
) -> PollResult:
    """Post one snapshot using the shared session, bounded in time and size."""
    try:
        async with asyncio.timeout(10), session.post(
            url, json=snapshot, headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        ) as response:
            if response.status == 401:
                return PollResult(Outcome.UNAUTHORIZED)
            if response.status == 409:
                return PollResult(Outcome.VERSION_REFUSED)
            if 500 <= response.status < 600:
                return PollResult(Outcome.SERVER_ERROR)
            if response.status != 200:
                return PollResult(Outcome.MALFORMED)
            # read(n) returns what has arrived so far, so collect chunks up to the cap.
            stream = response.content
            raw = b""
            while chunk := await stream.read(65537 - len(raw)):
                raw += chunk
                if len(raw) > 65536:
                    return PollResult(Outcome.MALFORMED)
            body = json.loads(raw)
            # Read the frozen version envelope before the version-specific body.
            if isinstance(body, dict) and type(body.get("api_version")) is int:
                if body["api_version"] > 0 and body["api_version"] not in SUPPORTED_API_VERSIONS:
                    return PollResult(Outcome.INCOMPATIBLE)
            return PollResult(Outcome.COUNTED, parse_summary(body))
    except TimeoutError:
        return PollResult(Outcome.TIMEOUT)
    except aiohttp.ClientError:
        return PollResult(Outcome.UNREACHABLE)
    except (ValueError, UnicodeError):
        return PollResult(Outcome.MALFORMED)
    except Exception as exc:  # noqa: BLE001 - only the type may be logged, never its text
        _LOGGER.debug("Heartbeat failed: %s", type(exc).__name__)
        return PollResult(Outcome.UNREACHABLE)
