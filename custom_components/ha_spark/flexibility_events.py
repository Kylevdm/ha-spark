"""The owner's durable opt-in to act on flexibility events (ADR-0013).

The options step stores the per-inverter export ratings; only the admin-only
``set_export_during_events`` owner service turns **Export during events** on,
because Home Assistant's options flow carries no trusted user id.
"""
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

OPTION_KEY = "flexibility_events"
Rating = Annotated[float, Field(gt=0, le=100000, allow_inf_nan=False)]


class FlexibilityEventsOptions(BaseModel):
    """Stored opt-in; the switch is on only with the admin and time that enabled it."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    export_during_events: bool = Field(default=False, strict=True)
    export_enabled_by: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    export_enabled_at: AwareDatetime | None = None
    export_ratings_w: dict[Annotated[str, Field(max_length=128)], Rating] = Field(
        default_factory=dict, max_length=64)


def stored(raw: object) -> FlexibilityEventsOptions:
    """Stored options, or the off default when missing or malformed."""
    if raw is None:
        return FlexibilityEventsOptions()
    try:
        options = FlexibilityEventsOptions.model_validate(raw)
    except ValidationError:
        return FlexibilityEventsOptions()
    if options.export_during_events and (
            options.export_enabled_by is None or options.export_enabled_at is None):
        # An enabled switch with no accountable admin is never on.
        return options.model_copy(update={"export_during_events": False})
    return options


def switched_off(raw: object) -> dict[str, Any]:
    """Keep ratings, but require the owner service to confirm consent again."""
    return stored(raw).model_copy(update={
        "export_during_events": False, "export_enabled_by": None, "export_enabled_at": None,
    }).model_dump(mode="json")


def changed_rated_claims(options: Mapping[str, Any], claims: object) -> set[str]:
    """Compare proposed gateway claims with the entry's previous claims, without rows."""
    from .native_export import claim_from_raw
    from .topology import Topology, validate_topology

    topology = validate_topology(options.get("topology"))
    if not isinstance(topology, Topology):
        return set()
    ratings = stored(options.get(OPTION_KEY)).export_ratings_w

    def claim_for(raw: object, host: str | None, port: int, unit: int) -> object:
        return next((claim_from_raw(item) for item in (raw if isinstance(raw, list) else [])
                     if isinstance(item, dict) and item.get("host") == host
                     and item.get("port") == port and item.get("unit_id") == unit), None)

    return {inverter.id for inverter in topology.inverters if inverter.id in ratings
            and claim_for(options.get("gateway_claims"), inverter.gateway_host,
                          inverter.gateway_port, inverter.gateway_unit_id)
            != claim_for(claims, inverter.gateway_host,
                         inverter.gateway_port, inverter.gateway_unit_id)}
