"""HA-free durable authority records; no write permission or recovery progress.

The integration store starts empty, never imports add-on authority, and becomes
an authority source only after the ownership handshake. HoldDevice deliberately
retains the legacy identity fields until the native hold port adds its identity.
"""

from datetime import time
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

OperatingMode = Literal["observe", "protect", "optimise"]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
Epoch = Annotated[int, Field(strict=True, ge=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid schema version")
        return value


class Transition(Record):
    target: OperatingMode
    started_at: AwareDatetime
    steps: Annotated[tuple[Text, ...], Field(max_length=64)] = ()
    failing_actions: Annotated[tuple[Text, ...], Field(max_length=64)] = ()


class HoldDevice(Record):
    driver: Text
    power_switch: Text
    hub: Annotated[str, Field(strict=True, max_length=128)]
    slave: Annotated[int, Field(strict=True, ge=0, le=247)]


class HoldRecord(Record):
    """A persisted request, not proof of a verified device write."""

    cause: Literal["ev", "dispatch"]
    started_at: AwareDatetime
    trusted_end: AwareDatetime | None = None
    device: HoldDevice
    requested: Literal["off"] = "off"
    confirmed: bool = Field(default=False, strict=True)
    owned: bool = Field(default=True, strict=True)
    pending_release: bool = Field(default=False, strict=True)
    clear_count: int = Field(default=0, ge=0, le=5, strict=True)
    last_clear_report_at: AwareDatetime | None = None
    evidence_stale_since: AwareDatetime | None = None
    degraded_notified: bool = Field(default=False, strict=True)
    remap_notified: bool = Field(default=False, strict=True)
    release_refused_since: AwareDatetime | None = None
    release_refused_reason: Literal["switch_unreadable", "schedule_unverified"] | None = None
    release_refused_notified: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_cause_fields(self) -> Self:
        if (self.cause == "dispatch") != (self.trusted_end is not None):
            raise ValueError("invalid hold end")
        if self.cause == "dispatch" and (self.clear_count or self.last_clear_report_at is not None):
            raise ValueError("invalid hold clearance")
        return self


class ManualSession(Record):
    # None means until resumed. A deadline is resolved once to a UTC instant.
    deadline: AwareDatetime | None = None
    return_to: Literal["protect", "optimise"]
    keep_protection: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def validate_utc(self) -> Self:
        offset = self.deadline.utcoffset() if self.deadline is not None else None
        if offset is not None and offset.total_seconds() != 0:
            raise ValueError("deadline must be UTC")
        return self


class FallbackState(Record):
    active: bool = Field(default=False, strict=True)
    causes: Annotated[
        tuple[Literal["addon_liveness", "soc_integrity"], ...], Field(max_length=2)
    ] = ()
    entered_at: AwareDatetime | None = None


class EventIdentity(Record):
    direction: Literal["import", "export"]
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("invalid event window")
        return self


class ExportWindow(Record):
    start: time
    end: time


class ExportSupervision(Record):
    inverter_id: Text
    id: UUID
    event: EventIdentity
    confirming_user_id: Text
    epoch: Epoch
    ends_at: AwareDatetime


class ExportRecord(Record):
    inverter_id: Text
    event: EventIdentity
    window: ExportWindow
    store_id: UUID
    epoch: Epoch
    supervision_id: UUID
    state: Literal["armed", "active", "preserved", "stop_pending", "cleared", "unknown"]
    last_reconcile_at: AwareDatetime
    stop_reason: Text | None = None
    cleanup_verified: bool = Field(default=False, strict=True)


class ExportTransition(Record):
    inverter_id: Text
    id: UUID
    event: EventIdentity
    at: AwareDatetime
    kind: Literal["accepted", "armed", "started", "cleanup_verified", "aborted",
                  "retryable_clock_hold", "tripped"]
    reason: Text | None = None


class StopLatch(Record):
    inverter_id: Text
    event: EventIdentity
    window_end: AwareDatetime
    epoch: Epoch


class AuthorityState(Record):
    schema_version: Literal[1] = 1
    store_id: UUID
    committed_mode: OperatingMode
    addon_session_id: Text | None = None
    epoch: Epoch
    transition: Transition | None = None
    manual_session: ManualSession | None = None
    fallback: FallbackState = Field(default_factory=FallbackState)
    holds: Annotated[tuple[HoldRecord, ...], Field(max_length=128)] = ()
    export_supervisions: Annotated[tuple[ExportSupervision, ...], Field(max_length=64)] = ()
    export_records: Annotated[tuple[ExportRecord, ...], Field(max_length=64)] = ()
    export_transitions: Annotated[tuple[ExportTransition, ...], Field(max_length=1024)] = ()
    stop_latches: Annotated[tuple[StopLatch, ...], Field(max_length=64)] = ()
    last_changed_at: AwareDatetime
    requester_kind: Literal["cli", "ingress", "system", "service", "repair"] = "system"


class AuthorityMarker(Record):
    schema_version: Literal[1]
    store_id: UUID
