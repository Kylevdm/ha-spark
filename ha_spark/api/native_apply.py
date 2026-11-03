"""Add-on device intents, bounded apply delivery and snapshot-only native SoC.

No hardware permission lives here. The integration checks every intent against
its local authority and reports verification; a timeout never resubmits work.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypedDict

from pydantic import BeforeValidator, Field, JsonValue, model_validator

from ha_spark.api.commands import CommandQueue, CommandResult, Record
from ha_spark.api.contract import IntegrationLink
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext
from ha_spark.devices.inverters.solis import solis_current_a
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.energy.models import ChargeIntent
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.topology import Topology, effective_limits, split_bank_allocation


def _instant(value: object) -> object:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value


Instant = Annotated[datetime, BeforeValidator(_instant)]


class CurrentIntent(Record):
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    current_a: float = Field(ge=0, le=62.5)


class ChargeCurrentLimit(CurrentIntent):
    kind: Literal["charge_current_limit"] = "charge_current_limit"


class ChargeWindow(CurrentIntent):
    kind: Literal["charge_window"] = "charge_window"
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ExportEvent(Record):
    direction: Literal["export"]
    start: Instant
    end: Instant

    @model_validator(mode="after")
    def validate_window(self) -> ExportEvent:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("invalid event window")
        if self.end <= self.start:
            raise ValueError("invalid event window")
        return self


class ExportInterval(Record):
    start: Instant
    end: Instant

    @model_validator(mode="after")
    def validate_window(self) -> ExportInterval:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("invalid export window")
        if self.end <= self.start:
            raise ValueError("invalid export window")
        return self


class ExportSoc(Record):
    ok: bool = Field(strict=True)
    soc_pct: float = Field(ge=0, le=100, allow_inf_nan=False)
    observed_at: Instant

    @model_validator(mode="after")
    def validate_aware(self) -> ExportSoc:
        if self.observed_at.utcoffset() is None:
            raise ValueError("invalid SoC time")
        return self


class ExportNone(Record):
    kind: Literal["export"] = "export"
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request: Literal["none"] = "none"


class ExportUntrusted(Record):
    kind: Literal["export"] = "export"
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request: Literal["untrusted"] = "untrusted"


class ExportWindow(Record):
    kind: Literal["export"] = "export"
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    request: Literal["window"] = "window"
    event: ExportEvent
    window: ExportInterval
    planned_export_kw: float = Field(gt=0, allow_inf_nan=False)
    dno_export_limit_kw: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    soc: ExportSoc


def _export_intent(
    intent: ChargeIntent, inverter_id: str,
) -> ExportNone | ExportUntrusted | ExportWindow:
    export = intent.export
    if export is None:
        if not intent.export_trusted:
            return ExportUntrusted(inverter_id=inverter_id)
        return ExportNone(inverter_id=inverter_id)
    direction, start, end = export.event_identity
    if direction != "export":
        raise ValueError("invalid export direction")
    dno = export.dno_export_limit_kw
    return ExportWindow(
        inverter_id=inverter_id,
        event=ExportEvent(direction="export", start=start, end=end),
        window=ExportInterval(start=export.window_start, end=export.window_end),
        planned_export_kw=export.planned_export_kw,
        dno_export_limit_kw=dno if dno > 0 else None,
        soc=ExportSoc(ok=intent.soc.ok, soc_pct=intent.soc.soc_now,
                      observed_at=intent.soc.observed_at),
    )


def build_intents(intent: ChargeIntent, settings: Settings, execution: ExecutionContext,
                  topology: Topology) -> list[dict[str, JsonValue]]:
    """Share the planner's single-bank current without multiplying it per inverter."""
    if not intent.soc.ok:
        return []
    limits = effective_limits(topology)
    bank = topology.banks[0]
    total_a = min(solis_current_a(intent, settings, execution), limits.banks[0].charge_a)
    allocations = split_bank_allocation(total_a, topology, limits, bank.id)
    intents: list[dict[str, JsonValue]] = []
    by_id = {inverter.id: inverter for inverter in topology.inverters}
    for allocation in allocations:
        inverter = by_id[allocation.inverter_id]
        if inverter.transport != "native" or allocation.charge_a is None:
            continue
        current = allocation.charge_a
        for action in (
            ChargeWindow(inverter_id=inverter.id, current_a=current,
                         start=intent.window_start.strftime("%H:%M"),
                         end=intent.window_end.strftime("%H:%M")),
            ChargeCurrentLimit(inverter_id=inverter.id, current_a=current),
            _export_intent(intent, inverter.id),
        ):
            intents.append(action.model_dump(mode="json"))
    return intents


def result_lines(result: CommandResult | None, count: int) -> list[ActionLine]:
    """Map positional intent results, retaining the legacy sequence-only fallback."""
    if result is not None and result.intents:
        if len(result.intents) != count:
            return result_lines(None, count)
        return [line for intent in result.intents for line in result_lines(
            CommandResult(session_id=result.session_id, sequence=result.sequence,
                          outcome=intent.outcome, reason=intent.reason), 1)]
    outcome = ActionOutcome.FAILED if result is None else {
        "applied": ActionOutcome.VERIFIED,
        "unchanged": ActionOutcome.UNCHANGED_VERIFIED,
        "deferred": ActionOutcome.BLOCKED,
        "refused": ActionOutcome.REFUSED,
        "unverified": ActionOutcome.UNVERIFIED,
        "failed": ActionOutcome.FAILED,
        "pending": ActionOutcome.FAILED,
    }[result.outcome]
    detail = "no result" if result is None or result.outcome == "pending" else (
        result.reason or result.outcome
    )
    prefix = "SKIP" if result is not None and result.outcome == "deferred" else outcome.name
    return [ActionLine(f"[{prefix}] native action: {detail}", outcome)
            for _ in range(max(1, count))]


class NativeApplyArgs(TypedDict, total=False):
    native_apply: NativeApply
    native_link: IntegrationLink
    native_epoch: int | None


class NativeApply:
    """One unresolved apply at a time, scoped to the add-on's in-memory queue."""

    def __init__(self, queue: CommandQueue) -> None:
        self.queue = queue
        self.sequence: int | None = None
        self.epoch: int | None = None
        self.stale_authority = False
        self._replanned_sequence: int | None = None

    @property
    def pending(self) -> bool:
        return self.sequence is not None and self.sequence in self.queue.pending

    def consume_stale_refusal(self, current_epoch: int | None) -> bool:
        """One fresh plan per stale refusal, only once a new epoch is known; never a replay."""
        result = self.queue.results.get(self.sequence) if self.sequence is not None else None
        stale = result is not None and (
            result.reason in ("stale_authority", "stale_epoch")
            or any(intent.reason == "stale_epoch" for intent in result.intents)
        )
        if (not stale or self.sequence == self._replanned_sequence
                or current_epoch is None or current_epoch == self.epoch):
            return False
        self._replanned_sequence = self.sequence
        return True

    async def send(self, intents: list[dict[str, JsonValue]], *, epoch: int) -> list[ActionLine]:
        self.stale_authority = False
        if self.pending:
            return [ActionLine("[SKIP] apply pending", ActionOutcome.BLOCKED)
                    for _ in range(max(1, len(intents)))]
        if not intents:
            return [ActionLine("[REFUSED] no native charge intents", ActionOutcome.REFUSED)]
        try:
            command = self.queue.enqueue(
                epoch=epoch, kind="apply", payload={"intents": list(intents)},
            )
        except ValueError:
            return [ActionLine("[FAILED] native apply unavailable", ActionOutcome.FAILED)]
        self.sequence, self.epoch = command.sequence, epoch
        result = await self.queue.wait(command.sequence)
        self.stale_authority = result is not None and (
            result.reason in ("stale_authority", "stale_epoch")
            or any(intent.reason == "stale_epoch" for intent in result.intents)
        )
        return result_lines(result, len(intents))


def snapshot_soc(link: IntegrationLink, now: datetime, settings: Settings) -> SocMeasurement:
    """Missing, stale or untrusted native data is never replaced with REST SoC."""
    unavailable = SocMeasurement(SocStatus.UNAVAILABLE, now)
    if (link.skew or link.v4 is None or link.v4.owner != "integration"
            or link.received_at is None or link.snapshot is None
            or not 0 <= (now - link.received_at).total_seconds() <= 90
            or abs((now - link.snapshot.sent_at).total_seconds()) > 300
            or link.topology.topology is None):
        return unavailable
    bank_id = link.topology.topology.banks[0].id
    reading = next((b.soc for b in link.v4.banks if b.id == bank_id), None)
    if reading is None or reading.read_at is None:
        return unavailable
    age = (now - reading.read_at).total_seconds()
    status = SocStatus(reading.reason)
    if reading.trust != "trusted" and status == SocStatus.OK:
        status = SocStatus.IMPLAUSIBLE
    if age < 0:
        status = SocStatus.REPORT_TIME_FUTURE
    elif age > settings.soc_max_report_age_minutes * 60:
        status = SocStatus.STALE
    return SocMeasurement(status, now, value=reading.value, reported_at=reading.read_at,
                          age_s=age)
