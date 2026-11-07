"""Pure export transition log: append-once, acknowledgement, trim and visibility."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from custom_components.ha_spark.authority_store import (
    AuthorityState,
    EventIdentity,
    ExportTransition,
)
from custom_components.ha_spark.native_export_log import (
    MAX_PER_INVERTER,
    MAX_TOTAL,
    TransitionKind,
    held_aborted,
    held_at_start,
    trim,
    visible,
    with_acks,
    with_append,
    with_pruned,
)

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
EVENT = EventIdentity(direction="export", start=NOW + timedelta(hours=2),
                      end=NOW + timedelta(hours=3))


def fresh() -> AuthorityState:
    return AuthorityState(store_id=uuid4(), committed_mode="optimise", epoch=1,
                          last_changed_at=NOW)


def test_one_row_per_inverter_event_and_kind_with_a_stable_id() -> None:
    state = with_append(fresh(), inverter_id="inv1", event=EVENT, kind="accepted", at=NOW)
    [row] = state.export_transitions
    again = with_append(state, inverter_id="inv1", event=EVENT, kind="accepted", at=NOW)
    assert again is state
    assert again.export_transitions == (row,)
    state = with_append(state, inverter_id="inv1", event=EVENT, kind="started", at=NOW)
    assert [item.kind for item in state.export_transitions] == ["accepted", "started"]
    other = EventIdentity(direction="export", start=EVENT.start + timedelta(days=1),
                          end=EVENT.end + timedelta(days=1))
    state = with_append(state, inverter_id="inv1", event=other, kind="accepted", at=NOW)
    assert len(state.export_transitions) == 3
    state = with_append(state, inverter_id="inv2", event=EVENT, kind="accepted", at=NOW)
    assert len(state.export_transitions) == 4


def test_acks_mark_only_matching_rows_and_prune_after_the_event() -> None:
    state = with_append(fresh(), inverter_id="inv1", event=EVENT, kind="started", at=NOW)
    [row] = state.export_transitions
    unknown = with_acks(state, [str(uuid4())], NOW)
    assert unknown is state
    acked = with_acks(state, [str(row.id)], NOW)
    assert acked.export_transitions[0].acked is True
    assert visible(acked.export_transitions, "inv1") == ()
    assert with_pruned(acked, EVENT.end - timedelta(seconds=1)) is acked
    assert with_pruned(acked, EVENT.end).export_transitions == ()


def test_trim_drops_acked_past_the_event_then_oldest_acked() -> None:
    ended = EventIdentity(direction="export", start=NOW - timedelta(hours=2),
                          end=NOW - timedelta(hours=1))
    rows = tuple(
        ExportTransition(inverter_id="inv1", id=uuid4(), event=ended, at=NOW,
                         kind="started", acked=True)
        for _ in range(3)
    ) + tuple(
        ExportTransition(inverter_id="inv1", id=uuid4(), event=EVENT, at=NOW,
                         kind="aborted", acked=True)
        for _ in range(MAX_PER_INVERTER + 1)
    )
    kept, dropped = trim(rows, NOW)
    assert len(kept) == MAX_PER_INVERTER and dropped == 0
    assert all(row.event == EVENT for row in kept)


def test_trim_keeps_each_inverter_under_its_cap_and_reports_unacked_drops() -> None:
    rows = tuple(
        ExportTransition(inverter_id="inv1", id=uuid4(), event=EVENT,
                         at=NOW + timedelta(seconds=index), kind="aborted")
        for index in range(MAX_PER_INVERTER)
    ) + tuple(
        ExportTransition(inverter_id="inv2", id=uuid4(), event=EVENT,
                         at=NOW + timedelta(seconds=index), kind="aborted")
        for index in range(2)
    )
    kept, dropped = trim(rows, NOW, reserve=1, reserve_inverter="inv1")
    assert dropped == 1
    assert sum(row.inverter_id == "inv1" for row in kept) == MAX_PER_INVERTER - 1
    assert sum(row.inverter_id == "inv2" for row in kept) == 2


def test_trim_never_exceeds_each_inverter_cap() -> None:
    rows = tuple(
        ExportTransition(inverter_id=f"inv{index % 8}", id=uuid4(), event=EVENT, at=NOW,
                         kind="aborted")
        for index in range(MAX_TOTAL)
    )
    kept, dropped = trim(rows, NOW, reserve=1, reserve_inverter="inv0")
    assert len(kept) == MAX_PER_INVERTER * 8 - 1
    assert dropped == MAX_TOTAL - len(kept)


def test_the_snapshot_offers_the_oldest_unacked_rows_first_and_bounded() -> None:
    from custom_components.ha_spark.native_export_log import SNAPSHOT_PER_INVERTER

    state = fresh()
    for day in range(SNAPSHOT_PER_INVERTER + 3):
        event = EventIdentity(direction="export", start=EVENT.start + timedelta(days=day),
                              end=EVENT.end + timedelta(days=day))
        state = with_append(state, inverter_id="inv1", event=event, kind="accepted", at=NOW)
    rows = visible(state.export_transitions, "inv1")
    assert rows == state.export_transitions[:SNAPSHOT_PER_INVERTER]
    acked = with_acks(state, [str(rows[0].id)], NOW)
    assert visible(acked.export_transitions, "inv1")[0] == state.export_transitions[1]


@pytest.mark.parametrize("settled", ["armed", "started", "aborted", "tripped"])
def test_held_at_start_excludes_settled_pairs(settled: TransitionKind) -> None:
    state = with_append(fresh(), inverter_id="inv1", event=EVENT, kind="unverified", at=NOW)
    assert held_at_start(state.export_transitions, EVENT.start - timedelta(seconds=1)) == ()
    assert held_at_start(state.export_transitions, EVENT.start) == (("inv1", EVENT),)
    state = with_append(state, inverter_id="inv1", event=EVENT, kind=settled, at=EVENT.start)
    assert held_at_start(state.export_transitions, EVENT.start) == ()


def test_held_at_start_is_scoped_to_inverter_and_event_and_deduplicates() -> None:
    other = EventIdentity(direction="export", start=EVENT.start - timedelta(days=1),
                          end=EVENT.end - timedelta(days=1))
    state = with_append(fresh(), inverter_id="inv1", event=EVENT, kind="unverified", at=NOW)
    state = with_append(state, inverter_id="inv2", event=EVENT, kind="armed", at=NOW)
    state = with_append(state, inverter_id="inv1", event=other, kind="started", at=NOW)
    rows = (*state.export_transitions, state.export_transitions[0])
    assert held_at_start(rows, EVENT.start) == (("inv1", EVENT),)


def test_an_armed_row_before_the_hold_does_not_settle_it() -> None:
    state = with_append(fresh(), inverter_id="inv1", event=EVENT, kind="armed", at=NOW)
    state = with_append(state, inverter_id="inv1", event=EVENT, kind="unverified", at=NOW)
    assert held_at_start(state.export_transitions, EVENT.start) == (("inv1", EVENT),)


def row(kind: TransitionKind, *, event: EventIdentity = EVENT, inverter: str = "inv1",
        acked: bool = False) -> ExportTransition:
    return ExportTransition(inverter_id=inverter, id=uuid4(), event=event, at=NOW,
                            kind=kind, acked=acked)


def test_held_aborted_needs_an_abort_after_the_hold() -> None:
    """An unheld pre-start abort never latches retroactively (astra, step 7)."""
    assert held_aborted((row("unverified"), row("aborted")), "inv1", EVENT)
    assert not held_aborted((row("aborted"), row("unverified")), "inv1", EVENT)
    assert not held_aborted((row("unverified"),), "inv1", EVENT)
    assert not held_aborted((row("unverified"), row("aborted", inverter="inv2")), "inv1", EVENT)
    other = EventIdentity(direction="export", start=EVENT.start + timedelta(days=1),
                          end=EVENT.end + timedelta(days=1))
    assert not held_aborted((row("unverified"), row("aborted", event=other)), "inv1", EVENT)


@pytest.mark.parametrize("acked", [True, False])
def test_trim_keeps_held_abort_evidence_until_the_event_ends(acked: bool) -> None:
    """The latch evidence outlives log pressure while its event is live."""
    filler = EventIdentity(direction="export", start=NOW - timedelta(hours=1),
                           end=NOW + timedelta(hours=5))
    rows = (row("unverified", acked=acked), row("aborted", acked=acked)) + tuple(
        row("accepted", event=filler, acked=acked) for _ in range(MAX_PER_INVERTER - 2))
    kept, _ = trim(rows, NOW, reserve=1, reserve_inverter="inv1")
    assert sum(r.inverter_id == "inv1" for r in kept) == MAX_PER_INVERTER - 1
    assert held_aborted(kept, "inv1", EVENT)
