"""The integration's half of the add-on/integration wire contract (#324).

Pure Python with no Home Assistant imports: this module holds the `api_version`
the integration speaks, the identity-only site snapshot it sends, and the summary
envelope it validates. Later tickets add the plan and authority display fields.
`api_version` is an integer, separate from the release version. A receiver may
ignore an unknown field only if ignoring it keeps
every meaning and safety obligation it already has. A field the receiver must act
on, a change of ownership or source of truth, or a change to what an absent field
means always bumps `api_version`. An optional field is not compatible by that fact
alone.

The add-on's matching constant is `ha_spark/api/contract.py`. Both suites
compare their constant with `tests/fixtures/contract/contract.json`, so neither
half can move a contract version alone.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

API_VERSION: Final = 1

# Every `api_version` this integration can send and accept replies to.
SUPPORTED_API_VERSIONS: Final = frozenset({1})


class MalformedSummary(ValueError):
    """The summary does not match the wire contract (no payload in the error)."""


@dataclass(frozen=True)
class Summary:
    """Validated summary envelope; plan fields arrive in later tickets."""

    api_version: int
    release_version: str
    session_id: str
    loop_tick: datetime | None
    built_at: datetime
    commands_pending: bool


def build_snapshot(*, site_id: str, release_version: str, sent_at: datetime) -> dict[str, object]:
    """Build the identity-only snapshot for api_version 1."""
    if str(UUID(site_id)) != site_id or sent_at.utcoffset() is None:
        raise ValueError("Invalid snapshot identity or time")
    return {
        "api_version": API_VERSION,
        "release_version": release_version,
        "site_id": site_id,
        "sent_at": sent_at.astimezone(UTC).isoformat(),
    }


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MalformedSummary()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MalformedSummary() from None
    if parsed.utcoffset() is None:
        raise MalformedSummary()
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        raise MalformedSummary() from None


def parse_summary(body: object) -> Summary:
    """Validate the envelope exactly, ignoring informational unknown fields."""
    if not isinstance(body, dict):
        raise MalformedSummary()
    version = body.get("api_version")
    release = body.get("release_version")
    session = body.get("session_id")
    pending = body.get("commands_pending")
    if (
        type(version) is not int or version < 1
        or not isinstance(release, str) or not 1 <= len(release) <= 64
        or not isinstance(session, str) or not 1 <= len(session) <= 64
        or type(pending) is not bool
        or "plan" not in body or (body["plan"] is not None and not isinstance(body["plan"], dict))
        or "loop_tick" not in body
    ):
        raise MalformedSummary()
    return Summary(
        api_version=version,
        release_version=release,
        session_id=session,
        loop_tick=None if body["loop_tick"] is None else _timestamp(body["loop_tick"]),
        built_at=_timestamp(body.get("built_at")),
        commands_pending=pending,
    )
