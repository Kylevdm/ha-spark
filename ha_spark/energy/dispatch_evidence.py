"""Pure evidence ladder for deciding whether a planned EV dispatch is credible.

Live dispatch state, an adjusted Octopus rate, or the car drawing power during
the planned slot confirms a dispatch. A connected plug corroborates it. A
readable disconnected plug contradicts it only when none of those stronger
signals applies; unset, unreadable, and unrecognised evidence leaves the
dispatch uncorroborated and therefore retained. After ten minutes in an active
slot, two readable power signals showing no car draw can contradict an otherwise
uncorroborated dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite

from ha_spark.energy.models import DispatchSlot


class DispatchRating(StrEnum):
    """The evidence rating assigned to one planned dispatch."""

    CONFIRMED = "Confirmed"
    CORROBORATED = "Corroborated"
    UNCORROBORATED = "Uncorroborated"
    CONTRADICTED = "Contradicted"


@dataclass(frozen=True)
class DispatchEvidence:
    """One read-only snapshot of evidence that can rate planned dispatches.

    ``None`` means an entity was unset, unreadable, or had no usable value.
    """

    live_dispatch: bool | None = None
    rate_adjusted: bool | None = None
    plug_value: str | None = None
    ev_status_value: str | None = None
    car_power_kw: float | None = None
    grid_import_kw: float | None = None
    forecast_house_kw: float | None = None


_CONNECTED_PLUG_STATES = {"ev connected", "charging", "waiting for ev"}
_DRAWING_EV_STATES = {"charging", "boosting", "delivering"}


def _normalise(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


def _slot_is_active(slot: DispatchSlot, now: datetime) -> bool:
    if (
        slot.start.utcoffset() is None
        or slot.end.utcoffset() is None
        or now.utcoffset() is None
    ):
        # HA sometimes supplies local wall times without an offset. When any
        # value is naive, compare all three on the same household wall clock.
        start = slot.start.replace(tzinfo=None)
        end = slot.end.replace(tzinfo=None)
        current = now.replace(tzinfo=None)
    else:
        start, end, current = slot.start, slot.end, now
    return start <= current < end


def rate_dispatch(
    slot: DispatchSlot, evidence: DispatchEvidence, now: datetime
) -> DispatchRating:
    """Rate one planned dispatch from the shared evidence snapshot.

    Live dispatch and rate-adjusted evidence confirm the whole dispatch
    picture. EV draw confirms only the slot active at ``now``. Plug evidence
    then corroborates or contradicts the planned slot.
    """
    if evidence.live_dispatch is True or evidence.rate_adjusted is True:
        return DispatchRating.CONFIRMED

    status = _normalise(evidence.ev_status_value)
    if status in _DRAWING_EV_STATES and _slot_is_active(slot, now):
        return DispatchRating.CONFIRMED

    def usable(value: float | None) -> bool:
        return value is not None and isfinite(value)

    car = evidence.car_power_kw
    grid = evidence.grid_import_kw
    house = evidence.forecast_house_kw
    residual = (
        grid - house
        if grid is not None and house is not None and usable(grid) and usable(house)
        else None
    )
    active = _slot_is_active(slot, now)
    if active and (
        (usable(car) and car is not None and car >= 1.4)
        or (residual is not None and residual >= 3.0)
    ):
        return DispatchRating.CONFIRMED

    plug = _normalise(evidence.plug_value)
    if plug in _CONNECTED_PLUG_STATES:
        return DispatchRating.CORROBORATED
    if plug == "ev disconnected":
        return DispatchRating.CONTRADICTED
    start, current = slot.start, now
    if start.utcoffset() is None or current.utcoffset() is None:
        start, current = start.replace(tzinfo=None), current.replace(tzinfo=None)
    if (
        active
        and current - start >= timedelta(minutes=10)
        and usable(car)
        and car is not None
        and 0 <= car < 1.4
        and residual is not None
        and residual < 3.0
    ):
        return DispatchRating.CONTRADICTED
    return DispatchRating.UNCORROBORATED


def partition_dispatches(
    slots: tuple[DispatchSlot, ...], evidence: DispatchEvidence, now: datetime
) -> tuple[tuple[DispatchSlot, ...], tuple[DispatchSlot, ...]]:
    """Rate each slot once and return the kept and contradicted partitions."""
    kept: list[DispatchSlot] = []
    dropped: list[DispatchSlot] = []
    for slot in slots:
        if rate_dispatch(slot, evidence, now) is DispatchRating.CONTRADICTED:
            dropped.append(slot)
        else:
            kept.append(slot)
    return tuple(kept), tuple(dropped)
