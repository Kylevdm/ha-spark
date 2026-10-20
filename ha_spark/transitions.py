"""Pure owner transition proposals; no proposal grants authority or performs I/O.

The daemon will persist and execute accepted proposals in a later ticket. An
unknown store can only produce a zero-step Observe confirmation intent: creating
a new store identity belongs to that future writer, not this pure function.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ha_spark.authority import AuthorityState, OperatingMode, Transition


@dataclass(frozen=True)
class PreconditionReport:
    """All unmet entry conditions per target; an empty tuple means pass."""

    protect: tuple[str, ...] = ()
    optimise: tuple[str, ...] = ()


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Manual control is representable but deliberately not accepted in phase 2.
    target: Literal["observe", "protect", "optimise", "manual"]
    expected_epoch: int | None = Field(ge=1, default=None)
    requester_kind: str
    requester_id: str = Field(min_length=1)


Refusal = Literal[
    "authority_unknown", "stale_epoch", "transition_in_progress",
    "preconditions_unmet", "not_owner", "no_change",
]


@dataclass(frozen=True)
class TransitionDecision:
    state: AuthorityState | None = None
    refusal: Refusal | None = None
    reasons: tuple[str, ...] = ()
    confirm_observe: bool = False

    @property
    def steps(self) -> tuple[str, ...]:
        return self.state.transition.steps if self.state and self.state.transition else ()


def propose_transition(
    state: AuthorityState | None,
    request: TransitionRequest,
    report: PreconditionReport,
    *,
    now: datetime,
) -> TransitionDecision:
    """Return a detached proposal, never mutate the current authority state.

    The caller must authenticate the requester before constructing the request.
    ``None`` denotes unknown authority; its epoch is also unknown (None).
    """
    if request.requester_kind not in ("cli", "ingress"):
        return TransitionDecision(refusal="not_owner")
    if state is None:
        if request.target != "observe":
            return TransitionDecision(refusal="authority_unknown")
        if request.expected_epoch is not None:
            return TransitionDecision(refusal="stale_epoch")
        return TransitionDecision(confirm_observe=True)
    if request.expected_epoch != state.epoch:
        return TransitionDecision(refusal="stale_epoch")
    if state.transition is not None:
        if request.target != "observe":
            return TransitionDecision(refusal="transition_in_progress")
        if state.transition.target == "observe":
            return TransitionDecision(state=state)
    if request.target == "manual":
        return TransitionDecision(refusal="preconditions_unmet",
                                  reasons=("manual control is not available in phase 2",))
    if state.transition is None and request.target == state.committed_mode:
        return TransitionDecision(refusal="no_change")
    target: OperatingMode = request.target
    failures: tuple[str, ...] = ()
    if target == "optimise":
        failures = tuple(dict.fromkeys((*report.protect, *report.optimise)))
    elif target == "protect" and state.committed_mode == "observe":
        failures = report.protect
    if failures:
        return TransitionDecision(refusal="preconditions_unmet", reasons=failures)
    steps: tuple[str, ...]
    if target == "observe":
        charging = (("cancel_export", "write_fallback")
                    if state.committed_mode == "optimise" else ())
        steps = charging + ("release_holds", "commit_observe")
    elif target == "optimise":
        steps = ("verify_fresh_plan",)
    elif state.committed_mode == "optimise":
        steps = ("cancel_export", "write_fallback")
    else:
        steps = ("evaluate_protection",)
    # Handover retains its source mode until verified. In particular, an Observe
    # request superseding Optimise -> Protect must still hand over charging.
    handover = target == "observe" or (
        state.committed_mode == "optimise" and target == "protect"
    )
    proposed = state.model_copy(update={
        "committed_mode": state.committed_mode if handover else target,
        "transition": Transition(target=target, started_at=now, steps=steps),
        "epoch": state.epoch + 1,
        "last_changed_at": now,
        "requester_kind": request.requester_kind,
    })
    return TransitionDecision(state=proposed)
