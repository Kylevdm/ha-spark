"""Strict command envelopes; fetched requests are never hardware permissions.

This pure module mirrors the add-on schema, pinned by shared golden fixtures.
Only the authority controller or a gated device handler may execute a command.
"""
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


class ReprobePayload(Requester):
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


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
                  "sync_clock": Requester, "reprobe": ReprobePayload, "apply": ApplyPayload,
                  "supervise_export": SupervisionPayload,
                  "end_supervision": EndSupervisionPayload}
        schemas[self.kind].model_validate(self.payload)
        return self


IntentOutcome = Literal["applied", "unchanged", "deferred", "refused", "unverified", "failed"]
WriteReason = Literal["not_available", "invalid_intent", "limits", "mode", "stale_epoch",
                      "authority_unknown", "gateway_not_owned", "read_only", "untrusted_soc",
                      "grid_charge_not_permitted"]


class IntentResult(Record):
    """Positionally matched to the request; no rejected values are echoed."""

    outcome: IntentOutcome
    reason: WriteReason | None = None
    written_a: float | None = Field(default=None, ge=0, le=62.5)


class ChargeCurrentLimit(Record):
    kind: Literal["charge_current_limit"]
    inverter_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    current_a: float = Field(ge=0, le=62.5)


class CommandResult(Record):
    session_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    outcome: Literal[
        "applied", "unchanged", "deferred", "refused", "unverified", "failed", "pending",
    ]
    reason: Literal["not_available", "stale_session", "stale_authority", "replayed",
                    "not_owner", "authority_unknown", "transition_in_progress", "stale_epoch",
                    "no_manual_session", "not_in_active_mode", "preconditions_unmet",
                    "invalid_household_zone", "native_device_missing", "invalid_intent", "limits",
                    "mode", "gateway_not_owned", "read_only", "untrusted_soc",
                    "grid_charge_not_permitted"] | None = None
    intents: list[IntentResult] = Field(default_factory=list, max_length=64)
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
