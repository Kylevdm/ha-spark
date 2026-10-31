"""Strict command envelopes and the add-on's in-memory, per-session queue.

The integration's matching pure schema is pinned by shared golden fixtures.
Only the authority controller or a gated device handler may execute a command.
"""
import asyncio
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Kind = Literal["transition", "manual", "sync_clock", "reprobe", "apply",
               "supervise_export", "end_supervision"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Requester(Record):
    requester_kind: Literal["cli", "ingress"]
    user_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def ingress_user(self) -> "Requester":
        if self.requester_kind == "ingress" and self.user_id is None:
            raise ValueError("missing requester")
        return self


class TransitionPayload(Requester):
    target: Literal["observe", "protect", "optimise"]
    expected_epoch: int = Field(ge=1)


class ManualPayload(Requester):
    target: Literal["manual", "resume"]
    expected_epoch: int = Field(ge=1)
    duration: Literal["one_hour", "until_tomorrow", "until_resumed"] = "one_hour"
    keep_protection: bool = True


class ApplyPayload(Record):
    intents: list[dict[str, JsonValue]] = Field(max_length=64)


class SupervisionPayload(Requester):
    event: dict[str, JsonValue]
    ratings_w: dict[str, float]


class EndSupervisionPayload(Requester):
    supervision_id: str = Field(min_length=1, max_length=128)


class Command(Record):
    session_id: str = Field(min_length=1, max_length=128)
    epoch: int = Field(ge=1)
    sequence: int = Field(ge=1)
    kind: Kind
    payload: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_payload(self) -> "Command":
        schemas: dict[str, type[BaseModel]] = {
                  "transition": TransitionPayload, "manual": ManualPayload,
                  "sync_clock": Requester, "reprobe": Requester, "apply": ApplyPayload,
                  "supervise_export": SupervisionPayload,
                  "end_supervision": EndSupervisionPayload}
        schemas[self.kind].model_validate(self.payload)
        return self


class CommandResult(Record):
    session_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    outcome: Literal[
        "applied", "unchanged", "deferred", "refused", "unverified", "failed", "pending",
    ]
    reason: Literal["not_available", "stale_session", "stale_authority", "replayed",
                    "not_owner", "authority_unknown", "transition_in_progress", "stale_epoch",
                    "no_manual_session", "not_in_active_mode", "preconditions_unmet",
                    "invalid_household_zone"] | None = None
    epoch: int | None = Field(default=None, ge=1)
    reasons: list[Literal[
        "devices_validated", "evidence_configured", "limits_confirmed", "fallback_valid",
        "live_session", "soc_trusted", "soc_recovered",
    ]] = Field(default_factory=list, max_length=7)


class FetchRequest(Record):
    session_id: str = Field(min_length=1, max_length=128)
    integration_session_id: str = Field(min_length=1, max_length=128)
    last_result_sequence: int = Field(ge=0)


class FetchReply(Record):
    session_id: str = Field(min_length=1, max_length=128)
    commands: list[Command] = Field(max_length=16)

    @model_validator(mode="after")
    def ordered(self) -> "FetchReply":
        last = 0
        for command in self.commands:
            if command.sequence <= last:
                raise ValueError("invalid command batch")
            last = command.sequence
        return self


class CommandQueue:
    """Per-boot pending work; a lost result post never acknowledges a command."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.integration_session_id: str | None = None
        self.sequence = 0
        self.pending: dict[int, Command] = {}
        self.results: dict[int, CommandResult] = {}
        self.offered: set[int] = set()
        self.changed = asyncio.Event()

    def enqueue(self, *, epoch: int, kind: Kind,
                payload: dict[str, JsonValue]) -> Command:
        if len(self.pending) >= 64:
            raise ValueError("command queue full")
        command = Command(session_id=self.session_id, epoch=epoch,
                          sequence=self.sequence + 1, kind=kind, payload=payload)
        if len(json.dumps(command.model_dump(), separators=(",", ":")).encode()) > 3800:
            raise ValueError("command too large")
        self.sequence = command.sequence
        self.pending[command.sequence] = command
        return command

    def fetch(self, request: FetchRequest) -> FetchReply:
        if request.session_id != self.session_id:
            raise ValueError("stale session")
        if (self.integration_session_id is not None
                and self.integration_session_id != request.integration_session_id):
            self.pending.clear()
            self.results.clear()
            self.offered.clear()
            self.changed.set()
        self.integration_session_id = request.integration_session_id
        commands = list(self.pending.values())[:16]
        self.offered.update(command.sequence for command in commands)
        reply = FetchReply(session_id=self.session_id, commands=commands)
        if len(json.dumps(reply.model_dump(), separators=(",", ":")).encode()) > 65536:
            raise ValueError("command batch too large")
        return reply

    def accept(self, result: CommandResult) -> None:
        if result.session_id != self.session_id or result.sequence not in self.offered:
            raise ValueError("invalid command result")
        if result.sequence in self.results:
            return
        if result.outcome == "pending":
            return
        self.pending.pop(result.sequence, None)
        self.results[result.sequence] = result
        while len(self.results) > 64:
            oldest = next(iter(self.results))
            del self.results[oldest]
            self.offered.discard(oldest)
        self.changed.set()

    async def wait(self, sequence: int) -> CommandResult | None:
        """Wait once, never cancel or resubmit the retained work on timeout."""
        try:
            async with asyncio.timeout(10):
                while sequence in self.pending:
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            return None
        return self.results.get(sequence)
