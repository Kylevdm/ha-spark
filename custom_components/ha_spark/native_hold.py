"""HA-free protection evidence policy and durable EV clearance rules.

Only advancing fresh reports clear an EV hold. Unknown evidence resets clearance,
including a release that has not yet verified. No sibling timestamp rescues a
stale source. Dispatch records retain their last trusted end through outages.
"""
from datetime import datetime, time, timedelta
from math import isfinite
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from .authority_store import HoldDevice, HoldRecord, Record
from .topology import EntityId, Inverter, Topology

MAX_AGE = 300
DEGRADED_AFTER = timedelta(minutes=60)
RELEASE_BACKOFF_AFTER = 5
RELEASE_BACKOFF = timedelta(minutes=10)
REPEAT_AFTER = timedelta(minutes=30)


class EvidenceSource(Record):
    source: Literal["entity", "native"] = "entity"
    entity_id: EntityId | None = None
    max_age_s: Annotated[float, Field(strict=True, gt=0, le=86400, allow_inf_nan=False)] = 300

    @model_validator(mode="after")
    def valid_source(self) -> Self:
        if (self.source == "entity") != (self.entity_id is not None):
            raise ValueError("invalid evidence source")
        return self


class EvidenceOptions(Record):
    ev_status: EvidenceSource | None = None
    battery_direction: EvidenceSource = EvidenceSource(source="native")
    ev_plug: EvidenceSource | None = None
    ev_power: EvidenceSource | None = None
    live_dispatch: EvidenceSource | None = None
    adjusted_rate: EvidenceSource | None = None
    dispatch_grid_power: EvidenceSource | None = None
    battery_discharge_sign: Literal["positive", "negative"] = "positive"
    dispatch_grid_invert: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def entity_sources(self) -> Self:
        if any(getattr(self, key) is not None and getattr(self, key).source != "entity"
               for key in SOURCES if key != "battery_direction"):
            raise ValueError("invalid evidence source")
        return self

    @property
    def configured(self) -> bool:
        return self.ev_status is not None

    def snapshot(self) -> dict[str, object]:
        return {key: source.model_dump(mode="json", exclude_none=True)
                for key in SOURCES if (source := getattr(self, key)) is not None}


SOURCES = ("ev_status", "battery_direction", "ev_plug", "ev_power", "live_dispatch",
           "adjusted_rate", "dispatch_grid_power")


def fresh(reported: datetime | None, now: datetime, age: float) -> bool:
    return (reported is not None and reported.utcoffset() is not None
            and timedelta(0) <= now - reported <= timedelta(seconds=age))


def hold_identity_valid(inverter: Inverter) -> bool:
    """The exact gateway identity must fit the durable hold record, never truncate."""
    return len(f"{inverter.gateway_host}:{inverter.gateway_port}") <= 128


def hold_device(inverter: Inverter) -> HoldDevice:
    return HoldDevice(driver="solis_native", power_switch=inverter.id,
                      hub=f"{inverter.gateway_host}:{inverter.gateway_port}",
                      slave=inverter.gateway_unit_id)


def remapped(device: HoldDevice, topology: Topology | None) -> bool:
    return topology is None or not any(
        inverter.transport == "native" and hold_identity_valid(inverter)
        and hold_device(inverter) == device
        for inverter in topology.inverters)


def advance_ev(record: HoldRecord, charging: bool | None,
               reported: datetime | None) -> HoldRecord:
    """The caller supplies None for unknown/stale EV observations."""
    if charging is None or charging or reported is None:
        return record.model_copy(update={"clear_count": 0, "last_clear_report_at": None,
                                         "pending_release": False})
    last = record.last_clear_report_at
    if last is not None and reported == last:
        return record
    if last is not None and reported < last:
        return record.model_copy(update={"clear_count": 0, "last_clear_report_at": None,
                                         "pending_release": False})
    count = min(5, record.clear_count + 1)
    return record.model_copy(update={"clear_count": count, "last_clear_report_at": reported,
                                     "pending_release": count == 5})


def charge_window(words: tuple[int, ...] | None) -> tuple[time, time] | None:
    if words is None or len(words) < 6:
        return None
    try:
        start, end = time(words[2], words[3]), time(words[4], words[5])
    except (ValueError, TypeError):
        return None
    return (start, end) if start != end else None


def in_window(now: time, start: time, end: time) -> bool:
    return start <= now < end if start < end else now >= start or now < end


def dispatch_kept(window: "DispatchWindow", now: datetime, *, live: bool | None = None,
                  adjusted: bool | None = None, status: str | None = None,
                  plug: str | None = None, car_kw: float | None = None,
                  grid_kw: float | None = None, house_kw: float | None = None) -> bool:
    """Port the shipped ladder; only contradicted dispatches are dropped.

    A native site has no planner house-load forecast. Missing that independent
    baseline leaves residual power unknown, never zero or an invented forecast.
    """
    if live is True or adjusted is True:
        return True
    active = window.start <= now < window.end
    status = status.strip().lower() if status is not None else None
    if active and status in ("charging", "boosting", "delivering"):
        return True
    car = car_kw if car_kw is not None and isfinite(car_kw) else None
    residual = grid_kw - house_kw if (
        grid_kw is not None and house_kw is not None
        and isfinite(grid_kw) and isfinite(house_kw)) else None
    if active and (car is not None and car >= 1.4 or residual is not None and residual >= 3):
        return True
    plug = plug.strip().lower() if plug is not None else None
    if plug in ("ev connected", "charging", "waiting for ev"):
        return True
    if plug == "ev disconnected":
        return False
    return not (active and now - window.start >= timedelta(minutes=10)
                and car is not None and 0 <= car < 1.4
                and residual is not None and residual < 3)


class DispatchWindow(Record):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("invalid dispatch window")
        return self
