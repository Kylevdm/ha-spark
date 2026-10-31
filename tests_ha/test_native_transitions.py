"""Native authority proposals use current evidence and never replay a saved plan."""
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from custom_components.ha_spark.authority_store import AuthorityState, Transition
from custom_components.ha_spark.transitions import (
    Preconditions,
    TransitionRequest,
    manual_deadline,
    propose_transition,
)

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def state(mode: str = "observe") -> AuthorityState:
    return AuthorityState.model_validate(dict(store_id=uuid4(), committed_mode=mode,
                                             epoch=1, last_changed_at=NOW))


def test_missing_native_sources_name_all_conditions() -> None:
    decision = propose_transition(state(), TransitionRequest(target="optimise"),
                                  Preconditions(), now=NOW)
    assert decision.state is None
    assert decision.reasons == ("devices_validated", "evidence_configured", "limits_confirmed",
                                "fallback_valid", "live_session", "soc_trusted", "soc_recovered")


def test_activation_grants_and_observe_supersedes() -> None:
    ready = Preconditions(True, True, True, True, True, True, True)
    decision = propose_transition(state(), TransitionRequest(target="optimise"), ready, now=NOW)
    assert decision.state is not None
    assert decision.state.committed_mode == "optimise"
    assert decision.state.transition is not None
    assert decision.state.transition.steps == ("verify_fresh_plan",)
    assert decision.state.epoch == 2
    observe = propose_transition(decision.state, TransitionRequest(target="observe"),
                                 Preconditions(), now=NOW)
    assert observe.state is not None and observe.state.committed_mode == "optimise"
    assert observe.state.transition is not None
    assert observe.state.transition.steps == ("cancel_export", "write_fallback", "release_holds")


def test_manual_supersedes_activation_and_mode_only_changes_return_to() -> None:
    activating = state("optimise").model_copy(update={"transition": Transition(
        target="optimise", started_at=NOW, steps=("verify_fresh_plan",))})
    manual = propose_transition(activating, TransitionRequest(target="manual"),
                                Preconditions(), now=NOW).state
    assert manual is not None and manual.manual_session is not None
    assert manual.manual_session.keep_protection
    assert manual.manual_session.deadline == NOW + timedelta(hours=1)
    changed = propose_transition(manual, TransitionRequest(target="protect"),
                                 Preconditions(), now=NOW).state
    assert changed is not None and changed.manual_session is not None
    assert changed.manual_session.return_to == "protect"
    assert changed.manual_session.deadline == manual.manual_session.deadline
    assert changed.transition == manual.transition


def test_deadlines_elapsed_time_gap_fold_and_same_morning() -> None:
    zone = ZoneInfo("Europe/London")
    assert manual_deadline("one_hour", NOW, zone) == NOW + timedelta(seconds=3600)
    assert manual_deadline("until_resumed", NOW, zone) is None
    morning = datetime(2026, 10, 30, 0, 30, tzinfo=UTC)
    assert manual_deadline("until_tomorrow", morning, zone) == morning.replace(hour=7, minute=0)
    spring = datetime(2026, 3, 29, 0, tzinfo=UTC)
    assert manual_deadline("until_tomorrow", spring, zone, time(1, 30)) == spring.replace(hour=1)
    autumn = datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert manual_deadline("until_tomorrow", autumn, zone, time(1, 30)) == autumn.replace(minute=30)


def test_manual_observe_preserves_owner_edits_and_resume_needs_current_work() -> None:
    manual = propose_transition(state("optimise"), TransitionRequest(target="manual"),
                                Preconditions(), now=NOW).state
    assert manual is not None
    # Export cancellation must complete before normal resume.
    manual = manual.model_copy(update={"transition": None})
    observe = propose_transition(manual, TransitionRequest(target="observe"),
                                 Preconditions(), now=NOW).state
    assert observe is not None and observe.manual_session is None
    assert observe.transition is not None
    assert observe.transition.steps == ("release_holds",)
    resume = propose_transition(manual, TransitionRequest(target="resume"),
                                Preconditions(), now=NOW).state
    assert resume is not None and resume.manual_session is None
    assert resume.transition is not None
    assert resume.transition.steps == ("verify_fresh_plan",)
    assert resume.epoch == manual.epoch + 1


def test_in_progress_only_observe_or_manual_may_supersede() -> None:
    active = state("optimise").model_copy(update={"transition": Transition(
        target="optimise", started_at=NOW, steps=("verify_fresh_plan",))})
    decision = propose_transition(active, TransitionRequest(target="protect"),
                                  Preconditions(), now=NOW)
    assert decision.state is None and decision.refusal == "transition_in_progress"
    stale = propose_transition(active, TransitionRequest(target="observe", expected_epoch=2),
                               Preconditions(), now=NOW)
    assert stale.state is None and stale.refusal == "stale_epoch"
