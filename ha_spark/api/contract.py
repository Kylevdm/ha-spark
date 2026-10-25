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
from ha_spark.config import Settings
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.v2l import V2LSession, delivered_fraction, savings

API_VERSION: Final = 1
SUPPORTED_API_VERSIONS: Final = frozenset({1})


class SiteSnapshot(BaseModel):
    """Validated v1 identity only; no authority or planning input is consumed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    api_version: int = Field(strict=True, ge=1)
    # Version characters only: health and the CLI print it, so no control or escape sequences.
    release_version: str = Field(
        strict=True, min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$",
    )
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


def link_view(link: IntegrationLink, now: datetime) -> dict[str, object]:
    """Allowlisted read-only diagnostics; liveness grants no authority."""
    snapshot = link.snapshot
    age = (now - link.received_at).total_seconds() if link.received_at is not None else None
    state = "never_connected"
    if link.skew:
        state = "version_skew"
    elif snapshot is not None:
        state = "connected" if age is not None and age <= 90 else "unreachable"
    return {
        "state": state,
        "site_id": snapshot.site_id if snapshot is not None else None,
        "integration_release_version": snapshot.release_version if snapshot is not None else None,
        "api_version": snapshot.api_version if snapshot is not None else None,
        "sent_at": snapshot.sent_at.isoformat() if snapshot is not None else None,
        "snapshot_age_s": max(0, int(age)) if snapshot is not None and age is not None else None,
    }


def version_refusal() -> dict[str, object]:
    """The frozen version envelope; never includes a version-specific body."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "supported_api_versions": sorted(SUPPORTED_API_VERSIONS),
    }


def plan_summary(
    *, session_id: str, built_at: datetime, loop_tick: datetime | None,
    plan: ChargePlan | None = None, plan_at: datetime | None = None,
    soc_observation_ok: bool = False, session: V2LSession | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Project only the envelope and allowlisted plan/session display numbers."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "session_id": session_id,
        "loop_tick": loop_tick.isoformat() if loop_tick is not None else None,
        "built_at": built_at.isoformat(),
        "commands_pending": False,
        "plan": plan_block(plan, plan_at, soc_observation_ok),
        "v2l": v2l_block(session, settings),
    }


def plan_block(
    plan: ChargePlan | None, computed_at: datetime | None, soc_observation_ok: bool,
) -> dict[str, object] | None:
    """No internal text, settings or device identifiers cross this projection."""
    if plan is None or computed_at is None:
        return None
    return {
        "computed_at": computed_at.isoformat(),
        "soc_trusted": plan.soc.ok and soc_observation_ok,
        "charge_needed_kwh": round(plan.required_kwh, 2),
        "target_soc_pct": round(plan.target_soc, 0),
        "forecast_load_kwh": round(plan.load_kwh, 2),
        "solar_forecast_kwh": round(plan.effective_solar_kwh, 2),
        "deficit_kwh": round(plan.deficit_kwh, 2),
        "planned_cost_gbp": round(plan.planned_cost, 2) if plan.planned_cost is not None else None,
        "baseline_cost_gbp": (
            round(plan.baseline_cost, 2) if plan.baseline_cost is not None else None
        ),
    }


def v2l_block(session: V2LSession | None, settings: Settings | None) -> dict[str, object] | None:
    """Session values use the existing savings and reset semantics."""
    if (settings is None or not settings.v2l_power_entity or session is None
            or session.last_sample_ts is None):
        return None
    avoided, refill, net = savings(
        session.kwh_delivered, settings.v2l_peak_rate_gbp, settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency, delivered_fraction(settings),
    )
    return {
        "energy_kwh": round(session.kwh_delivered, 2), "net_saving_gbp": round(net, 2),
        "avoided_gbp": round(avoided, 2), "refill_cost_gbp": round(refill, 2),
        "peak_power_w": round(session.peak_power_w, 0),
    }
