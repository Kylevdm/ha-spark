"""The integration's half of the add-on/integration wire contract (#324).

Pure Python with no Home Assistant imports: this module holds the `api_version`
the integration speaks, the identity-only site snapshot it sends, and the summary
and plan numbers it validates. Later tickets add authority display fields.
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

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Final, Literal, cast
from uuid import UUID

API_VERSION: Final = 1

# Every `api_version` this integration can send and accept replies to.
SUPPORTED_API_VERSIONS: Final = frozenset({1})


class MalformedSummary(ValueError):
    """The summary does not match the wire contract (no payload in the error)."""


Action = Literal["hold", "export", "charge", "idle"]


@dataclass(frozen=True)
class PlanStatusBlock:
    """Current intended action and safe display attributes."""

    action: Action
    reason: str
    model: str
    strategy: str
    soc_status: str
    soc_reason: str
    export_soc_needed_pct: float | None
    export_soc_needed_at: datetime | None


@dataclass(frozen=True)
class NextChange:
    """First scheduled change in intended action."""

    at: datetime
    action: Action
    reason: str


@dataclass(frozen=True)
class PlanBlock:
    """Allowlisted plan display values, never planning inputs."""

    computed_at: datetime
    soc_trusted: bool
    charge_needed_kwh: float | None
    target_soc_pct: float | None
    forecast_load_kwh: float | None
    solar_forecast_kwh: float | None
    deficit_kwh: float | None
    planned_cost_gbp: float | None
    baseline_cost_gbp: float | None
    status: PlanStatusBlock
    next_change: NextChange | None


@dataclass(frozen=True)
class V2LBlock:
    """Current V2L session totals."""

    energy_kwh: float
    net_saving_gbp: float
    avoided_gbp: float
    refill_cost_gbp: float
    peak_power_w: float


@dataclass(frozen=True)
class Summary:
    """Validated envelope and allowlisted plan/session display blocks."""

    api_version: int
    release_version: str
    session_id: str
    loop_tick: datetime | None
    built_at: datetime
    commands_pending: bool
    plan: PlanBlock | None
    v2l: V2LBlock | None


def build_snapshot(
    *, site_id: str, release_version: str, sent_at: datetime, api_version: int = API_VERSION,
) -> dict[str, object]:
    """Build the identity-only snapshot for api_version 1."""
    if str(UUID(site_id)) != site_id or sent_at.utcoffset() is None:
        raise ValueError("Invalid snapshot identity or time")
    return {
        "api_version": api_version,
        "release_version": release_version,
        "site_id": site_id,
        "sent_at": sent_at.astimezone(UTC).isoformat(),
    }


@dataclass(frozen=True)
class VersionEnvelope:
    """Frozen version metadata, safe to display even for an unsupported body."""

    api_version: int
    release_version: str
    supported_api_versions: frozenset[int] | None = None


def parse_version_envelope(body: object) -> VersionEnvelope:
    """Validate the frozen envelope before inspecting version-specific fields."""
    if not isinstance(body, dict):
        raise MalformedSummary()
    version = body.get("api_version")
    release = body.get("release_version")
    if (
        type(version) is not int or version < 1
        or type(release) is not str
        or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", release) is None
    ):
        raise MalformedSummary()
    return VersionEnvelope(version, release)


def parse_refusal(body: object) -> VersionEnvelope:
    """Validate a refusal without retaining unknown fields or error contents."""
    envelope = parse_version_envelope(body)
    assert isinstance(body, dict)
    supported = body.get("supported_api_versions")
    if (
        type(supported) is not list or not 1 <= len(supported) <= 16
        or any(type(version) is not int or version < 1 for version in supported)
        or len(set(supported)) != len(supported)
    ):
        raise MalformedSummary()
    return VersionEnvelope(envelope.api_version, envelope.release_version, frozenset(supported))


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
        or "loop_tick" not in body or "v2l" not in body
    ):
        raise MalformedSummary()
    return Summary(
        api_version=version,
        release_version=release,
        session_id=session,
        loop_tick=None if body["loop_tick"] is None else _timestamp(body["loop_tick"]),
        built_at=_timestamp(body.get("built_at")),
        commands_pending=pending,
        plan=_plan(body["plan"]),
        v2l=_v2l(body["v2l"]),
    )


def _number(value: object) -> float:
    # Exact types: bool and other subclasses are refused before conversion.
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        raise MalformedSummary()
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise MalformedSummary() from None
    if not isfinite(number):
        raise MalformedSummary()
    return number


def _plan(value: object) -> PlanBlock | None:
    if value is None:
        return None
    if not isinstance(value, dict) or type(value.get("soc_trusted")) is not bool:
        raise MalformedSummary()
    numbers: dict[str, float | None] = {}
    for key in ("charge_needed_kwh", "target_soc_pct", "forecast_load_kwh", "solar_forecast_kwh",
                "deficit_kwh", "planned_cost_gbp", "baseline_cost_gbp"):
        if key not in value:
            raise MalformedSummary()
        numbers[key] = None if value[key] is None else _number(value[key])
    if "next_change" not in value:
        raise MalformedSummary()
    return PlanBlock(computed_at=_timestamp(value.get("computed_at")),
                     soc_trusted=value["soc_trusted"], status=_status(value.get("status")),
                     next_change=_next_change(value["next_change"]), **numbers)


def _text(value: object, *, code: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 255 or not value.isprintable():
        raise MalformedSummary()
    if code and re.fullmatch(r"[a-z_]{1,32}", value) is None:
        raise MalformedSummary()
    return value


def _action(value: object) -> Action:
    if not isinstance(value, str) or value not in {"hold", "export", "charge", "idle"}:
        raise MalformedSummary()
    return cast(Action, value)


def _status(value: object) -> PlanStatusBlock:
    if not isinstance(value, dict):
        raise MalformedSummary()
    if "export_soc_needed_pct" not in value or "export_soc_needed_at" not in value:
        raise MalformedSummary()
    pct, at = value["export_soc_needed_pct"], value["export_soc_needed_at"]
    if (pct is None) != (at is None):
        raise MalformedSummary()
    return PlanStatusBlock(
        action=_action(value.get("action")), reason=_text(value.get("reason")),
        model=_text(value.get("model"), code=True),
        strategy=_text(value.get("strategy"), code=True),
        soc_status=_text(value.get("soc_status"), code=True),
        soc_reason=_text(value.get("soc_reason")),
        export_soc_needed_pct=None if pct is None else _number(pct),
        export_soc_needed_at=None if at is None else _timestamp(at),
    )


def _next_change(value: object) -> NextChange | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MalformedSummary()
    return NextChange(at=_timestamp(value.get("at")), action=_action(value.get("action")),
                      reason=_text(value.get("reason")))


def _v2l(value: object) -> V2LBlock | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MalformedSummary()
    return V2LBlock(**{key: _number(value.get(key)) for key in (
        "energy_kwh", "net_saving_gbp", "avoided_gbp", "refill_cost_gbp", "peak_power_w",
    )})
