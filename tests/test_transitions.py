"""Pure transition proposals never persist or execute their handover steps."""
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ha_spark.authority import AuthorityState, Transition
from ha_spark.transitions import PreconditionReport, TransitionRequest, propose_transition

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def state(mode="observe", transition=None):
    return AuthorityState(
        schema_version=1, store_id=UUID(int=1), committed_mode=mode, transition=transition,
        epoch=7, holds=(), fallback={}, readiness_notice={}, last_changed_at=NOW,
        requester_kind="system",
    )


def request(target, **kwargs):
    return TransitionRequest(target=target, expected_epoch=7, requester_kind="cli",
                             requester_id="cli", **kwargs)


@pytest.mark.parametrize("source,target,steps,committed", [
    ("observe", "protect", ("evaluate_protection",), "protect"),
    ("observe", "optimise", ("verify_fresh_plan",), "optimise"),
    ("protect", "optimise", ("verify_fresh_plan",), "optimise"),
    ("optimise", "protect", ("cancel_export", "write_fallback"), "optimise"),
    ("protect", "observe", ("release_holds", "commit_observe"), "protect"),
    ("optimise", "observe", ("cancel_export", "write_fallback", "release_holds",
                              "commit_observe"), "optimise"),
])
def test_transition_rows(source, target, steps, committed):
    original = state(source)
    result = propose_transition(original, request(target), PreconditionReport(), now=NOW)
    assert result.refusal is None
    assert result.state.committed_mode == committed
    assert result.state.epoch == 8
    assert result.steps == steps
    assert result.state.transition.target == target
    assert original.epoch == 7 and original.transition is None


@pytest.mark.parametrize("target,kind,epoch,pending,failures,reason", [
    ("protect", "system", 7, None, (), "not_owner"),
    ("protect", "agent", 7, None, (), "not_owner"),
    ("protect", "cli", 6, None, (), "stale_epoch"),
    ("protect", "cli", 7, "optimise", (), "transition_in_progress"),
    ("protect", "cli", 7, None, ("model", "evidence"), "preconditions_unmet"),
    ("observe", "cli", 7, None, (), "no_change"),
    ("manual", "cli", 7, None, (), "preconditions_unmet"),
])
def test_transition_refusals_have_no_steps(target, kind, epoch, pending, failures, reason):
    original = state(transition=Transition(target=pending, started_at=NOW) if pending else None)
    req = TransitionRequest(target=target, expected_epoch=epoch, requester_kind=kind,
                            requester_id="owner")
    result = propose_transition(original, req, PreconditionReport(protect=failures), now=NOW)
    assert result.refusal == reason
    assert result.steps == () and result.state is None
    if failures:
        assert result.reasons == failures


@pytest.mark.parametrize("target", ["observe", "protect", "optimise"])
def test_unknown_only_confirms_observe_without_steps(target):
    req = TransitionRequest(target=target, expected_epoch=None, requester_kind="cli",
                            requester_id="cli")
    result = propose_transition(None, req, PreconditionReport(), now=NOW)
    assert result.confirm_observe is (target == "observe")
    assert result.refusal == (None if target == "observe" else "authority_unknown")
    assert result.state is None and result.steps == ()


def test_observe_supersedes_activation_and_repeated_request_joins():
    original = state("optimise", Transition(target="optimise", started_at=NOW))
    result = propose_transition(original, request("observe"), PreconditionReport(), now=NOW)
    assert result.steps == ("cancel_export", "write_fallback", "release_holds", "commit_observe")
    joined = propose_transition(result.state, TransitionRequest(
        target="observe", expected_epoch=8, requester_kind="ingress", requester_id="owner"),
        PreconditionReport(), now=NOW)
    assert joined.state == result.state
    assert joined.state.epoch == 8


@pytest.mark.parametrize("source", ["protect", "optimise"])
def test_same_active_mode_is_no_change(source):
    result = propose_transition(state(source), request(source), PreconditionReport(), now=NOW)
    assert result.refusal == "no_change" and result.steps == ()


def test_observe_superseding_charging_handover_keeps_all_required_steps():
    handover = propose_transition(state("optimise"), request("protect"),
                                  PreconditionReport(), now=NOW)
    result = propose_transition(handover.state, TransitionRequest(
        target="observe", expected_epoch=8, requester_kind="cli", requester_id="cli"),
        PreconditionReport(), now=NOW)
    assert result.steps == ("cancel_export", "write_fallback", "release_holds", "commit_observe")


def test_deescalation_does_not_require_entry_preconditions():
    result = propose_transition(state("optimise"), request("protect"),
                                PreconditionReport(protect=("model unavailable",)), now=NOW)
    assert result.refusal is None
