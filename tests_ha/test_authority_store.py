"""Pure authority records reject malformed persisted authority without HA I/O."""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from custom_components.ha_spark.authority_store import (
    AuthorityState,
    EventIdentity,
    ExportRecord,
    ExportSupervision,
    ExportTransition,
    ExportWindow,
    FallbackState,
    HoldDevice,
    HoldRecord,
    ManualSession,
    StopLatch,
    Transition,
)

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def fresh() -> AuthorityState:
    return AuthorityState(store_id=uuid4(), committed_mode="observe", epoch=1,
                          last_changed_at=NOW)


def test_authority_round_trip() -> None:
    state = fresh()
    assert AuthorityState.model_validate_json(state.model_dump_json()) == state
    assert not state.holds and not state.export_records and not state.export_supervisions
    assert not state.export_transitions and not state.stop_latches
    assert state.manual_session is None and not state.fallback.active


def test_full_authority_contents_round_trip() -> None:
    state = fresh()
    event = EventIdentity(direction="export", start=NOW, end=NOW + timedelta(hours=1))
    supervision = ExportSupervision(inverter_id="inverter", id=uuid4(), event=event,
                                    confirming_user_id="user-id", epoch=1, ends_at=event.end)
    complete = AuthorityState.model_validate({
        **state.model_dump(),
        "transition": Transition(target="optimise", started_at=NOW,
                                 steps=("clear_export", "apply"), failing_actions=("apply",)),
        "manual_session": ManualSession(return_to="optimise", deadline=event.end),
        "fallback": FallbackState(active=True, causes=("addon_liveness", "soc_integrity"),
                                  entered_at=NOW),
        "export_supervisions": (supervision,),
        "export_records": (ExportRecord(
            inverter_id="inverter", event=event, window=ExportWindow(start=time(12), end=time(13)),
            store_id=state.store_id, epoch=1, supervision_id=supervision.id,
            state="stop_pending", last_reconcile_at=NOW, stop_reason="tripped",
        ),),
        "export_transitions": (ExportTransition(inverter_id="inverter", id=uuid4(),
                                                 event=event, at=NOW, kind="tripped"),),
        "stop_latches": (StopLatch(inverter_id="inverter", event=event,
                                   window_end=event.end, epoch=1),),
    })
    assert AuthorityState.model_validate_json(complete.model_dump_json()) == complete


@pytest.mark.parametrize("updates", [
    {"epoch": True}, {"epoch": "1"}, {"epoch": 0}, {"schema_version": True},
    {"committed_mode": "manual"},
    {"last_changed_at": "2026-10-30T12:00:00"}, {"holds": [None] * 129},
    {"transition": {"target": "observe", "started_at": NOW, "steps": ["x"] * 65}},
    {"manual_session": {"deadline": "2026-10-30T12:00:00", "return_to": "protect"}},
    {"fallback": {"causes": ["x" * 129]}}, {"unexpected": "PRIVATE-CANARY"},
])
def test_authority_rejects_invalid_records(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        AuthorityState.model_validate({**fresh().model_dump(), **updates})
    assert "PRIVATE-CANARY" not in str(error.value)


def test_hold_fields_and_cause_validation() -> None:
    device = HoldDevice(driver="solis", power_switch="select.power", hub="", slave=1)
    hold = HoldRecord(cause="ev", started_at=NOW, device=device, confirmed=True,
                      owned=True, pending_release=True, clear_count=4,
                      last_clear_report_at=NOW, evidence_stale_since=NOW,
                      degraded_notified=True, remap_notified=True, release_refused_since=NOW,
                      release_refused_reason="schedule_unverified", release_refused_notified=True)
    state = fresh().model_copy(update={"holds": (hold,)})
    assert AuthorityState.model_validate_json(state.model_dump_json()) == state
    for updates in ({"cause": "dispatch"}, {"trusted_end": NOW}, {"clear_count": 6}):
        with pytest.raises(ValidationError):
            HoldRecord.model_validate({**hold.model_dump(), **updates})
