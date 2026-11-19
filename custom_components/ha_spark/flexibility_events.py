"""The owner's durable opt-in to act on flexibility events (ADR-0013).

The options step stores the per-inverter export ratings; only the admin-only
``set_export_during_events`` owner service turns **Export during events** on,
because Home Assistant's options flow carries no trusted user id.
"""
from typing import Annotated

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
