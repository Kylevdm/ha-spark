"""The integration's half of the add-on/integration wire contract (#324).

Pure Python with no Home Assistant imports: this module holds the `api_version`
the integration speaks, the site snapshot it sends, and the summary
and plan numbers and read-only authority mirrors it validates.
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

API_VERSION: Final = 4
# Display codes the add-on sends at this api_version (its planner, SoC check and fallback).
PLAN_MODELS: Final = frozenset({"daily", "slots"})
PLAN_STRATEGIES: Final = frozenset({"deficit", "fill"})
SOC_STATUSES: Final = frozenset({
    "ok", "implausible", "read_failed", "unavailable", "malformed", "not_finite",
    "out_of_range", "report_time_unusable", "report_time_future", "stale", "unchanged_too_long",
})
FALLBACK_CAUSES: Final = frozenset({"soc_integrity"})

# Every `api_version` this integration can send and accept replies to.
SUPPORTED_API_VERSIONS: Final = frozenset({1, 2, 3, 4})


class MalformedSummary(ValueError):
    """The summary does not match the wire contract (no payload in the error)."""


Action = Literal["hold", "export", "charge", "idle"]


@dataclass(frozen=True)
class PlanStatusBlock:
    """Current intended action and safe display attributes."""

    action: Action | None  # None: the add-on couldn't resolve the plan's times
    reason: str | None
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
class FallbackBlock:
    """Site fallback verdict, without per-device details."""

    active: bool
    causes: tuple[str, ...]


@dataclass(frozen=True)
class HoldsBlock:
    """One hold verdict per cause, not per device."""

    ev: bool
    dispatch: bool
    trusted_end: datetime | None


@dataclass(frozen=True)
class AuthorityBlock:
    """Read-only mirrors; this is not integration-owned authority."""

    mode: str
    transition_target: str | None
    last_changed_at: datetime | None
    save_failed: bool
    fallback: FallbackBlock
    holds: HoldsBlock


@dataclass(frozen=True)
class AddonAuthority:
    """Handshake facts only; malformed or missing means unknown."""

    active: Literal["active", "retired"]
    mode: str
    holds_present: bool
    store_state: Literal["ok", "unknown", "save_failed"]


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
    authority: AuthorityBlock
    charge_window_start: datetime | None = None
    household_timezone: str | None = None
    addon_authority: AddonAuthority | None = None
    legacy_overlay: dict[str, bool] | None = None
    export_acks: tuple[str, ...] | None = None


def build_snapshot(
    *, site_id: str, release_version: str, sent_at: datetime, api_version: int,
    solar_forecast: object = None, topology: object = None,
    major_loads: object = None, supply_guard: object = None,
) -> dict[str, object]:
    """Build only fields supported by the negotiated api_version."""
    if str(UUID(site_id)) != site_id or sent_at.utcoffset() is None:
        raise ValueError("Invalid snapshot identity or time")
    result: dict[str, object] = {
        "api_version": api_version,
        "release_version": release_version,
        "site_id": site_id,
        "sent_at": sent_at.astimezone(UTC).isoformat(),
    }
    if api_version >= 2:
        result["solar_forecast"] = solar_forecast
    if api_version >= 3:
        if api_version == 3 and isinstance(topology, dict):
            document = topology.get("topology")
            if isinstance(document, dict) and isinstance(document.get("inverters"), list):
                # Old receivers reject unknown topology fields. Transport is a
                # v4 handshake input, never an implicit upgrade of a v3 peer.
                topology = topology | {"topology": document | {"inverters": [
                    {key: value for key, value in inverter.items()
                     if key not in {"transport", "gateway_host", "gateway_port", "gateway_unit_id"}}
                    if isinstance(inverter, dict) else inverter
                    for inverter in document["inverters"]
                ]}}
        result["topology"] = topology
        result["major_loads"] = major_loads
        if supply_guard is not None:
            result["supply_guard"] = supply_guard
    if api_version >= 4:
        result["authority_owner"] = "addon"
    return result


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


# fromisoformat alone also takes other separators and out-of-range offset minutes.
_ISO_8601 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _timestamp(value: object) -> datetime:
    if type(value) is not str or _ISO_8601.fullmatch(value) is None:
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
    window = None
    if version >= 2:
        try:
            window = _timestamp(body.get("charge_window_start"))
        except MalformedSummary:
            pass
    zone = body.get("household_timezone") if version >= 3 else None
    if not isinstance(zone, str) or re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", zone) is None:
        zone = None
    return Summary(
        api_version=version,
        release_version=release,
        session_id=session,
        loop_tick=None if body["loop_tick"] is None else _timestamp(body["loop_tick"]),
        built_at=_timestamp(body.get("built_at")),
        commands_pending=pending,
        plan=_plan(body["plan"]),
        v2l=_v2l(body["v2l"]),
        authority=_authority(body.get("authority")),
        charge_window_start=window,
        household_timezone=zone,
        addon_authority=_addon_authority(body.get("addon_authority")) if version >= 4 else None,
        legacy_overlay=_legacy_overlay(body.get("legacy_overlay")) if version >= 4 else None,
        export_acks=_export_acks(body.get("export_acks")) if version >= 4 else None,
    )


def _addon_authority(value: object) -> AddonAuthority | None:
    if not isinstance(value, dict):
        return None
    active, mode, state = value.get("active"), value.get("mode"), value.get("store_state")
    if (type(active) is not str or active not in {"active", "retired"}
            or type(mode) is not str
            or mode not in {"observe", "protect", "optimise", "authority_unknown"}
            or type(value.get("holds_present")) is not bool
            or type(state) is not str or state not in {"ok", "unknown", "save_failed"}):
        return None
    return AddonAuthority(
        cast(Literal["active", "retired"], active), mode, value["holds_present"],
        cast(Literal["ok", "unknown", "save_failed"], state),
    )


def _legacy_overlay(value: object) -> dict[str, bool] | None:
    if (not isinstance(value, dict) or len(value) > 8
            or any(type(key) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) is None
                   or type(flag) is not bool for key, flag in value.items())):
        return None
    return dict(value)


def _export_acks(value: object) -> tuple[str, ...] | None:
    if (not isinstance(value, list) or len(value) > 512
            or any(type(key) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) is None
                   for key in value) or len(set(value)) != len(value)):
        return None
    return tuple(value)


def _authority(value: object) -> AuthorityBlock:
    if not isinstance(value, dict):
        raise MalformedSummary()
    mode, target = value.get("mode"), value.get("transition_target")
    modes = {"observe", "protect", "optimise"}
    if (type(mode) is not str or mode not in modes | {"authority_unknown"}
            or "transition_target" not in value
            or (target is not None and (type(target) is not str or target not in modes))):
        raise MalformedSummary()
    fallback, holds = value.get("fallback"), value.get("holds")
    if not isinstance(fallback, dict) or not isinstance(holds, dict):
        raise MalformedSummary()
    ev, dispatch = holds.get("ev"), holds.get("dispatch")
    if not isinstance(ev, dict) or not isinstance(dispatch, dict):
        raise MalformedSummary()
    causes = fallback.get("causes")
    if (type(causes) is not list or len(causes) > 8
            or any(type(cause) is not str or re.fullmatch(r"[a-z_]{1,32}", cause) is None
                   for cause in causes)):
        raise MalformedSummary()
    # Only known cause codes reach an attribute; a newer add-on's extra code is dropped.
    causes = [cause for cause in causes if cause in FALLBACK_CAUSES]
    if ("last_changed_at" not in value or "trusted_end" not in dispatch
            or type(value.get("save_failed")) is not bool
            or type(fallback.get("active")) is not bool
            or type(ev.get("active")) is not bool or type(dispatch.get("active")) is not bool):
        raise MalformedSummary()
    return AuthorityBlock(
        mode=mode, transition_target=target,
        last_changed_at=(None if value["last_changed_at"] is None
                         else _timestamp(value["last_changed_at"])),
        save_failed=value["save_failed"],
        fallback=FallbackBlock(fallback["active"], tuple(causes)),
        holds=HoldsBlock(ev["active"], dispatch["active"],
                         None if dispatch["trusted_end"] is None
                         else _timestamp(dispatch["trusted_end"])),
    )


# Physical bounds where they are certain; other figures (deficit, costs, net saving)
# can legitimately be negative.
_RANGES: Final = {
    "target_soc_pct": (0.0, 100.0), "export_soc_needed_pct": (0.0, 100.0),
    "energy_kwh": (0.0, None), "avoided_gbp": (0.0, None), "refill_cost_gbp": (0.0, None),
    "peak_power_w": (0.0, None),
}


def _ranged(key: str, value: object) -> float:
    number = _number(value)
    low, high = _RANGES.get(key, (None, None))
    if (low is not None and number < low) or (high is not None and number > high):
        raise MalformedSummary()
    return number


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
        numbers[key] = None if value[key] is None else _ranged(key, value[key])
    if "next_change" not in value:
        raise MalformedSummary()
    return PlanBlock(computed_at=_timestamp(value.get("computed_at")),
                     soc_trusted=value["soc_trusted"], status=_status(value.get("status")),
                     next_change=_next_change(value["next_change"]), **numbers)


def _text(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 255 or not value.isprintable():
        raise MalformedSummary()
    return value


def _code(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise MalformedSummary()
    return value


def _action(value: object) -> Action:
    if type(value) is not str or value not in {"hold", "export", "charge", "idle"}:
        raise MalformedSummary()
    return cast(Action, value)


def _status(value: object) -> PlanStatusBlock:
    if not isinstance(value, dict):
        raise MalformedSummary()
    if "export_soc_needed_pct" not in value or "export_soc_needed_at" not in value:
        raise MalformedSummary()
    pct, at = value["export_soc_needed_pct"], value["export_soc_needed_at"]
    if (pct is None) != (at is None) or "action" not in value or "reason" not in value:
        raise MalformedSummary()
    action, reason = value["action"], value["reason"]
    if (action is None) != (reason is None):
        raise MalformedSummary()
    return PlanStatusBlock(
        action=None if action is None else _action(action),
        reason=None if reason is None else _text(reason),
        model=_code(value.get("model"), PLAN_MODELS),
        strategy=_code(value.get("strategy"), PLAN_STRATEGIES),
        soc_status=_code(value.get("soc_status"), SOC_STATUSES),
        soc_reason=_text(value.get("soc_reason")),
        export_soc_needed_pct=None if pct is None else _ranged("export_soc_needed_pct", pct),
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
    return V2LBlock(**{key: _ranged(key, value.get(key)) for key in (
        "energy_kwh", "net_saving_gbp", "avoided_gbp", "refill_cost_gbp", "peak_power_w",
    )})
