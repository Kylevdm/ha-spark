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

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from ha_spark import __version__
from ha_spark.api.plan_status import PlanStatus, resolve
from ha_spark.authority import AuthorityState, HoldRecord
from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.soc_integrity import SocStatus
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


@dataclass(frozen=True)
class AuthorityInputs:
    """The authority store as the daemon holds it, for allowlisted projections only."""

    state: AuthorityState | None
    known: bool
    save_failed: bool


def plan_summary(
    *, session_id: str, built_at: datetime, loop_tick: datetime | None,
    plan: ChargePlan | None = None, plan_at: datetime | None = None,
    soc_observation_ok: bool = False, session: V2LSession | None = None,
    settings: Settings | None = None, authority: AuthorityInputs | None = None,
) -> dict[str, object]:
    """Project only the envelope and allowlisted plan/session display numbers."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "session_id": session_id,
        "loop_tick": loop_tick.isoformat() if loop_tick is not None else None,
        "built_at": built_at.isoformat(),
        "commands_pending": False,
        "plan": plan_block(
            plan, plan_at, soc_observation_ok, built_at,
            settings.timezone if settings is not None else "UTC",
            authority.state.holds if authority is not None and authority.state is not None else (),
        ),
        "v2l": v2l_block(session, settings),
        "authority": authority_block(authority or AuthorityInputs(None, False, False)),
    }


_RAW_STATE_STATUSES: Final = {
    SocStatus.UNAVAILABLE: "SoC sensor unavailable",
    SocStatus.MALFORMED: "SoC state is not a number",
    SocStatus.NOT_FINITE: "SoC state is not finite",
}


def _instant(value: datetime | None) -> str | None:
    """ISO 8601 for an aware instant the integration can normalise to UTC, else None."""
    if value is None or value.utcoffset() is None:
        return None
    try:
        value.astimezone(UTC)
    except OverflowError:
        return None
    return value.isoformat()


def authority_block(authority: AuthorityInputs) -> dict[str, object]:
    """Aggregate store facts only; never relay per-device records or driver text."""
    state = authority.state if authority.known else None
    transition = state.transition if state else None
    dispatch_ends = [hold.trusted_end for hold in state.holds
                     if hold.cause == "dispatch" and hold.trusted_end is not None] if state else []
    return {
        "mode": state.committed_mode if state else "authority_unknown",
        "transition_target": transition.target if transition else None,
        "last_changed_at": _instant(state.last_changed_at) if state else None,
        "save_failed": authority.save_failed,
        "fallback": {
            "active": state.fallback.active if state else False,
            "causes": sorted({cause for cause in state.fallback.causes
                              if cause in {"soc_integrity"}}) if state else [],
        },
        "holds": {
            "ev": {"active": any(h.cause == "ev" for h in state.holds) if state else False},
            "dispatch": {
                "active": any(h.cause == "dispatch" for h in state.holds) if state else False,
                "trusted_end": _instant(max(dispatch_ends)) if dispatch_ends else None,
            },
        },
    }


def _finite(value: object, ndigits: int) -> float | None:
    """A rounded finite number, else None: one bad value never fails the heartbeat."""
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        return None
    try:
        rounded = round(float(value), ndigits)
    except OverflowError:
        return None
    return rounded if math.isfinite(rounded) else None


def plan_block(
    plan: ChargePlan | None, computed_at: datetime | None, soc_observation_ok: bool,
    now: datetime, timezone: str, holds: tuple[HoldRecord, ...],
) -> dict[str, object] | None:
    """Allowlisted numbers and status; never device identifiers or driver text."""
    if plan is None or computed_at is None:
        return None
    # An unloadable zone degrades to UTC, as everywhere else, never a failed heartbeat;
    # unrepresentable plan times leave the action unknown rather than fail it.
    status: PlanStatus | None
    try:
        status = resolve(plan, holds, now, load_timezone(timezone))
    except (ArithmeticError, TypeError, ValueError):
        status = None
    if plan.soc.status in _RAW_STATE_STATUSES:
        # These reasons quote the SoC entity's raw state, which is household text.
        soc_reason = _RAW_STATE_STATUSES[plan.soc.status]
    else:
        try:
            soc_reason = "".join(c for c in plan.soc.reason if c.isprintable())[:200]
        except (ArithmeticError, TypeError, ValueError):
            soc_reason = ""
    export_pct = _finite(plan.export_soc_needed_pct, 0)
    export_at = _instant(plan.export_soc_needed_at) if export_pct is not None else None
    return {
        "computed_at": computed_at.isoformat(),
        "soc_trusted": plan.soc.ok and soc_observation_ok,
        "charge_needed_kwh": _finite(plan.required_kwh, 2),
        "target_soc_pct": _finite(plan.target_soc, 0),
        "forecast_load_kwh": _finite(plan.load_kwh, 2),
        "solar_forecast_kwh": _finite(plan.effective_solar_kwh, 2),
        "deficit_kwh": _finite(plan.deficit_kwh, 2),
        "planned_cost_gbp": _finite(plan.planned_cost, 2),
        "baseline_cost_gbp": _finite(plan.baseline_cost, 2),
        "status": {
            "action": status.action if status else None,
            "reason": status.reason if status else None,
            "model": plan.model if plan.model in {"daily", "slots"} else "daily",
            "strategy": plan.strategy if plan.strategy in {"deficit", "fill"} else "deficit",
            "soc_status": plan.soc.status.value,
            # The reason can embed the SoC entity's raw state: printable, bounded, never empty.
            "soc_reason": soc_reason or plan.soc.status.value,
            "export_soc_needed_pct": export_pct if export_at is not None else None,
            "export_soc_needed_at": export_at,
        },
        "next_change": {
            "at": status.next_at.isoformat(), "action": status.next_action,
            "reason": status.next_reason,
        } if status is not None and status.next_at is not None else None,
    }


def v2l_block(session: V2LSession | None, settings: Settings | None) -> dict[str, object] | None:
    """Session values use the existing savings and reset semantics.

    The session file is the add-on's own but unvalidated: any non-number or
    non-finite value, input or result, makes the block null rather than invented.
    """
    if (settings is None or not settings.v2l_power_entity or session is None
            or session.last_sample_ts is None):
        return None
    kwh, peak = _finite(session.kwh_delivered, 6), _finite(session.peak_power_w, 6)
    if kwh is None or peak is None:
        return None
    avoided, refill, net = savings(
        kwh, settings.v2l_peak_rate_gbp, settings.v2l_offpeak_rate_gbp,
        settings.v2l_round_trip_efficiency, delivered_fraction(settings),
    )
    block: dict[str, object] = {
        "energy_kwh": _finite(kwh, 2), "net_saving_gbp": _finite(net, 2),
        "avoided_gbp": _finite(avoided, 2), "refill_cost_gbp": _finite(refill, 2),
        "peak_power_w": _finite(peak, 0),
    }
    return None if None in block.values() else block
