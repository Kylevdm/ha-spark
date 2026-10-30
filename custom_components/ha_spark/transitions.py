"""Pure native authority proposals and household-clock manual deadlines.

Proposals are not write permission. The controller persists and verifies a proposal
before executing its steps. Missing native precondition sources always fail closed.
"""
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from .authority_store import AuthorityState, ManualSession, OperatingMode, Record, Transition

_DEFAULT_ZONE = ZoneInfo("Europe/London")

Duration = Literal["one_hour", "until_tomorrow", "until_resumed"]


@dataclass(frozen=True)
class Preconditions:
    devices_validated: bool = False
    evidence_configured: bool = False
    limits_confirmed: bool = False
    fallback_valid: bool = False
    live_session: bool = False
    soc_trusted: bool = False
    soc_recovered: bool = False

    def unmet(self, *, optimise: bool) -> tuple[str, ...]:
        names: tuple[str, ...] = ("devices_validated", "evidence_configured", "limits_confirmed")
        if optimise:
            names += ("fallback_valid", "live_session", "soc_trusted", "soc_recovered")
        return tuple(name for name in names if not getattr(self, name))


class TransitionRequest(Record):
    target: Literal["observe", "protect", "optimise", "manual", "resume"]
    expected_epoch: int | None = Field(default=None, strict=True, ge=1)
    requester_kind: Literal["cli", "ingress", "service", "system"] = "system"
    duration: Duration = "one_hour"
    keep_protection: bool = Field(default=True, strict=True)


@dataclass(frozen=True)
class TransitionDecision:
    state: AuthorityState | None = None
    refusal: str | None = None
    reasons: tuple[str, ...] = ()


def manual_deadline(
    duration: Duration, now: datetime, zone: ZoneInfo, at: time = time(7),
) -> datetime | None:
    """Resolve once: first fold, or the first valid minute after a spring gap."""
    now = now.astimezone(UTC)
    if duration == "until_resumed":
        return None
    if duration == "one_hour":
        return now + timedelta(seconds=3600)
    day = now.astimezone(zone).date()
    for offset in (0, 1):
        wall = datetime.combine(day + timedelta(days=offset), at.replace(tzinfo=None))
        while True:
            candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == wall:
                break
            wall = (wall + timedelta(minutes=1)).replace(second=0, microsecond=0)
        if candidate > now:
            return candidate
    raise ValueError("invalid deadline")


def propose_transition(
    state: AuthorityState | None, request: TransitionRequest, report: Preconditions,
    *, now: datetime, zone: ZoneInfo = _DEFAULT_ZONE, at: time = time(7),
) -> TransitionDecision:
    """Return a detached grant or handover; refusals have no candidate to save."""
    if state is None:
        return TransitionDecision(refusal="authority_unknown")
    if ((request.expected_epoch is not None or request.requester_kind in ("cli", "ingress"))
            and request.expected_epoch != state.epoch):
        return TransitionDecision(refusal="stale_epoch")
    target = request.target
    manual = state.manual_session
    # Changing the return-to mode is not a new charging grant or a handover.
    if manual is not None and target in ("protect", "optimise"):
        if target == manual.return_to:
            return TransitionDecision(state=state)
        return TransitionDecision(state=state.model_copy(update={
            "manual_session": manual.model_copy(update={"return_to": target}),
            "epoch": state.epoch + 1, "last_changed_at": now,
            "requester_kind": request.requester_kind,
        }))
    if state.transition is not None and target not in ("observe", "manual"):
        return TransitionDecision(refusal="transition_in_progress")
    steps: tuple[str, ...] = ()
    mode = state.committed_mode
    transition_target: OperatingMode
    if target == "manual":
        if mode == "observe" or manual is not None:
            return TransitionDecision(refusal="not_in_active_mode")
        manual = ManualSession(return_to=mode, keep_protection=request.keep_protection,
                               deadline=manual_deadline(request.duration, now, zone, at))
        steps = ("cancel_export",) if mode == "optimise" else ()
        if not manual.keep_protection:
            steps += ("release_holds",)
        transition_target = mode
    elif target == "resume":
        if manual is None:
            return TransitionDecision(refusal="no_manual_session")
        mode = manual.return_to
        manual = None
        # Never replay an earlier plan, even when resuming without a live session.
        steps = (("write_fallback",) if state.fallback.causes else ("verify_fresh_plan",)) \
            if mode == "optimise" else ("evaluate_protection",)
        transition_target = mode
    elif target == "observe":
        if ((mode == "observe" and state.transition is None)
                or (state.transition is not None and state.transition.target == "observe")):
            return TransitionDecision(state=state)
        if manual is None and mode == "optimise":
            steps = ("cancel_export", "write_fallback")
        elif state.transition is not None and "cancel_export" in state.transition.steps:
            # Manual handover is not complete until export cancellation verifies.
            # Retain that obligation, but never restore charging over manual edits.
            steps = ("cancel_export",)
        steps += ("release_holds",)
        manual = None
        transition_target = "observe"
    else:
        if target == mode and state.transition is None:
            return TransitionDecision(state=state)
        failures = report.unmet(optimise=target == "optimise") \
            if target == "optimise" or mode == "observe" else ()
        if failures:
            return TransitionDecision(refusal="preconditions_unmet", reasons=failures)
        transition_target = target
        if target == "optimise":
            mode = "optimise"
            steps = ("verify_fresh_plan",)
        elif mode == "optimise":
            steps = ("cancel_export", "write_fallback")
        else:
            mode = "protect"
            steps = ("evaluate_protection",)
    return TransitionDecision(state=state.model_copy(update={
        "committed_mode": mode, "manual_session": manual,
        "transition": (Transition(target=transition_target, started_at=now, steps=steps)
                       if steps else None),
        "epoch": state.epoch + 1, "last_changed_at": now,
        "requester_kind": request.requester_kind,
    }))
