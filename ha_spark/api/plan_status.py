"""Pure, read-only resolution of intended actions on the household clock.

Store holds are current facts, not the plan's older EV observation. This display
projection grants no authority and never changes a hold or an execution input.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from ha_spark.authority import HoldRecord
from ha_spark.energy.horizon import _window_bounds
from ha_spark.energy.models import ChargePlan

Action = Literal["hold", "export", "charge", "idle"]


@dataclass(frozen=True)
class PlanStatus:
    """The current intended action and first future change, if scheduled."""

    action: Action
    reason: str
    next_at: datetime | None
    next_action: Action | None
    next_reason: str | None


def _number(value: object) -> float | None:
    """A finite float, else None: odd plan numbers degrade the display, never raise."""
    if not isinstance(value, (int, float)) or type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _amount(value: object, unit: str, ndigits: int) -> str | None:
    """A bounded figure for a reason sentence, else None (the sentence drops it)."""
    number = _number(value)
    if number is None or abs(number) >= 1e5:
        return None
    return f"{number:.{ndigits}f}{unit}"


def resolve(
    plan: ChargePlan, holds: Sequence[HoldRecord], now: datetime, tz: ZoneInfo,
) -> PlanStatus:
    """Resolve half-open windows using planner geometry and fixed safe sentences."""
    now = now.astimezone(UTC)
    intent = plan.charge_intent
    today = now.astimezone(tz).date()
    charge = tuple(_window_bounds(today + timedelta(days=d), intent.window_start,
                                  intent.window_end, tz) for d in (-1, 0, 1, 2))
    # Occurrences are only enumerated this far; a later boundary can't be resolved
    # (with equal bounds the windows are back to back and charging never stops).
    horizon = max(end for _, end in charge)
    required = _number(plan.required_kwh)
    needs_charge = required is not None and required > 0
    dispatch = tuple((s.astimezone(UTC), e.astimezone(UTC)) for s, e in intent.holds)
    export = intent.export
    # A record still awaiting release has no scheduled end once its end has passed.
    indefinite = any(h.cause == "ev" or h.trusted_end is not None and h.trusted_end <= now
                     for h in holds)
    store_ends = [h.trusted_end.astimezone(UTC) for h in holds
                  if h.trusted_end is not None and h.trusted_end > now]

    def clock(t: datetime) -> str:
        return t.astimezone(tz).strftime("%H:%M")

    def action_at(t: datetime) -> tuple[Action, str]:
        ends = [e for s, e in dispatch if s <= t < e] + [e for e in store_ends if t < e]
        if indefinite or ends:
            if any(h.cause == "ev" for h in holds):
                return "hold", "Battery held while the EV charges."
            # An expired record is still held; its trusted end is descriptive only.
            end = max(ends or [h.trusted_end for h in holds if h.trusted_end is not None])
            return "hold", f"Battery held for a smart-charge dispatch until {clock(end)}."
        if export is not None and (
            export.window_start.astimezone(UTC) <= t < export.window_end.astimezone(UTC)
        ):
            return "export", (
                f"Exporting for the flexibility event until {clock(export.window_end)}."
            )
        if needs_charge and any(s <= t < e for s, e in charge):
            reserve_reason = f" {plan.reserve_limit_reason}" if plan.reserve_limit_reason else ""
            if plan.charge_funded_reservations:
                reservation = plan.charge_funded_reservations[0]
                phrase = {
                    "reach-next-cheap-slot": "the run to the next cheap slot",
                    "axle-export-event": "the flexibility export",
                    "reach-charge-window": "the run to the charge window",
                }.get(reservation.obligation_kind, "planned demand")
                kwh = _amount(required, " kWh", 1)
                return "charge", (
                    f"Charging {kwh or 'the battery'} to cover {phrase}.{reserve_reason}"
                )
            end = next(e for s, e in charge if s <= t < e)
            kwh, soc = _amount(required, " kWh", 1), _amount(plan.target_soc, "%", 0)
            return "charge", (f"Charging {kwh or 'the battery'} to reach "
                              f"{soc or 'the target'} by {clock(end)}.{reserve_reason}")
        return "idle", "Nothing to charge, export or hold right now."

    action, reason = action_at(now)
    boundaries = {b for window in (*charge, *dispatch) for b in window}
    boundaries.update(store_ends)
    if export is not None:
        boundaries.update((export.window_start.astimezone(UTC), export.window_end.astimezone(UTC)))
    if not indefinite:
        for boundary in sorted(b for b in boundaries if now < b < horizon):
            next_action, next_reason = action_at(boundary)
            if next_action != action:
                return PlanStatus(action, reason, boundary, next_action, next_reason)
    return PlanStatus(action, reason, None, None, None)
