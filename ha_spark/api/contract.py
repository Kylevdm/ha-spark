"""The add-on's half of the add-on/integration wire contract (#324).

`api_version` is an integer, separate from the release version. A receiver may
ignore an unknown field only if ignoring it keeps every meaning and safety
obligation it already has. A field the receiver must act on, a change of ownership
or source of truth, or a change to what an absent field means always bumps
`api_version`. An optional field is not compatible by that fact alone.

The integration's matching constant is `custom_components/ha_spark/wire.py`.
Both suites compare their constant with `tests/fixtures/contract/contract.json`,
so neither half can move a contract version alone.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ha_spark import __version__

API_VERSION: Final = 1
SUPPORTED_API_VERSIONS: Final = frozenset({1})


class SiteSnapshot(BaseModel):
    """Validated v1 identity only; no authority or planning input is consumed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: int = Field(strict=True, ge=1)
    release_version: str = Field(strict=True, min_length=1, max_length=64)
    site_id: str = Field(strict=True)
    sent_at: AwareDatetime

    @field_validator("site_id")
    @classmethod
    def canonical_site_id(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("sent_at", mode="before")
    @classmethod
    def string_timestamp(cls, value: object) -> object:
        # Pydantic parses the string (RFC 3339, strict offsets); refuse its unix-number form.
        if not isinstance(value, str):
            raise ValueError("timestamp must be a string")
        return value

    @field_validator("sent_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except OverflowError:
            # Pydantic wraps only ValueError: year 1 at +14:00 would otherwise escape as a 500.
            raise ValueError("timestamp out of range") from None


@dataclass(frozen=True)
class IntegrationLink:
    """Last accepted snapshot and outcome, held only in daemon memory."""

    snapshot: SiteSnapshot | None
    received_at: datetime | None
    skew: bool


def version_refusal() -> dict[str, object]:
    """The frozen version envelope; never includes a version-specific body."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "supported_api_versions": sorted(SUPPORTED_API_VERSIONS),
    }


def plan_summary(
    *, session_id: str, built_at: datetime, loop_tick: datetime | None,
) -> dict[str, object]:
    """Project the phase-4 envelope without any household state."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "session_id": session_id,
        "loop_tick": loop_tick.isoformat() if loop_tick is not None else None,
        "built_at": built_at.isoformat(),
        "commands_pending": False,
        "plan": None,
    }
