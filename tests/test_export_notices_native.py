"""Native export lifecycle notices driven by the integration transition log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ha_spark.api.contract import IntegrationLink
from ha_spark.api.server import AppState
from ha_spark.api.snapshot_v4 import Event, Export, ExportTransition, SnapshotV4
from ha_spark.config import Settings
from ha_spark.energy.native_export_notices import event_id, notify_export_transitions

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
START = NOW + timedelta(hours=2)
END = NOW + timedelta(hours=3)


class FakeRest:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.failures = failures

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[object]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("notification failed")
        self.calls.append((domain, service, data or {}))
        return []


def settings(tmp_path: Path, *, service: str = "mobile_app_phone") -> Settings:
    return Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db"),
                    notify_service=service)


def wire_event(start: datetime = START, end: datetime = END) -> Event:
    return Event.model_validate({"direction": "export", "start": start.isoformat(),
                                 "end": end.isoformat()})


def transition(identifier: str, kind: str, *, reason: str | None = None,
               cleared: bool | None = None, start: datetime = START,
               end: datetime = END) -> ExportTransition:
    return ExportTransition.model_validate({
        "id": identifier, "kind": kind, "event": {
            "direction": "export", "start": start.isoformat(), "end": end.isoformat(),
        }, "at": NOW.isoformat(), "reason": reason, "cleared": cleared,
    })


def link(*entries: Export) -> IntegrationLink:
    return IntegrationLink(None, None, False,
                           v4=SnapshotV4(owner="integration", export=entries))


def entry(*rows: ExportTransition) -> Export:
    return Export(inverter_id="inv1", bound=None, supervision=None, record=None,
                  stop_latch=None, transitions=rows)


async def test_kinds_map_to_the_existing_notice_contract(tmp_path: Path) -> None:
    rows = (
        transition("a", "accepted"),
        transition("b", "armed"),
        transition("c", "started"),
        transition("d", "cleanup_verified"),
        transition("e", "clock_hold", reason="clock_not_trusted"),
        transition("f", "aborted", reason="not_supervised", cleared=True),
    )
    tripped_event = event_id(wire_event(START + timedelta(days=1),
                                       END + timedelta(days=1)))
    entry_two = Export(
        inverter_id="inv2", bound=None, supervision=None, record=None, stop_latch=None,
        transitions=(transition("g", "tripped", start=START + timedelta(days=1),
                                end=END + timedelta(days=1)),))
    rest = FakeRest()
    acknowledged: set[str] = set()

    def planned(identity: str) -> tuple[float | None, float | None]:
        return (3.2, 7.36) if identity == event_id(wire_event()) else (None, None)

    await notify_export_transitions(settings(tmp_path), rest,  # type: ignore[arg-type]
                                    link(entry(*rows), entry_two),
                                    acknowledged=acknowledged, planned_for=planned)

    titles = [call[2]["title"] for call in rest.calls]
    assert titles == [
        "Axle export accepted", "Axle export started", "Axle export cleanup verified",
        "Axle export held: inverter clock", "Axle export aborted", "Axle export aborted",
    ]
    accepted, _started, _cleanup, clock, aborted, tripped = rest.calls
    assert "3.2 kW" in accepted[2]["message"] and "7.36 kW" in accepted[2]["message"]
    assert "clock_not_trusted" in clock[2]["message"]
    assert "not_supervised" in aborted[2]["message"]
    assert "no export window remains programmed" in aborted[2]["message"]
    assert "site export over limit" in tripped[2]["message"]
    assert tripped[2]["message"].startswith("Aborted Axle export")
    assert tripped_event not in accepted[2]["message"]
    assert acknowledged == {"a", "b", "c", "d", "e", "f", "g"}


async def test_a_failed_notice_is_retried_and_then_acknowledged(tmp_path: Path) -> None:
    rest = FakeRest(failures=1)
    acknowledged: set[str] = set()
    body = link(entry(transition("started", "started")))
    await notify_export_transitions(settings(tmp_path), rest, body,  # type: ignore[arg-type]
                                    acknowledged=acknowledged,
                                    planned_for=lambda _: (None, None))
    assert acknowledged == set() and rest.calls == []
    await notify_export_transitions(settings(tmp_path), rest, body,  # type: ignore[arg-type]
                                    acknowledged=acknowledged,
                                    planned_for=lambda _: (None, None))
    assert acknowledged == {"started"}
    assert len(rest.calls) == 1


async def test_an_outage_across_a_transition_notifies_each_once(tmp_path: Path) -> None:
    body = link(entry(transition("accepted", "accepted")))
    rest = FakeRest()
    first: set[str] = set()
    await notify_export_transitions(settings(tmp_path), rest, body,  # type: ignore[arg-type]
                                    acknowledged=first,
                                    planned_for=lambda _: (None, None))
    assert first == {"accepted"} and len(rest.calls) == 1
    # The add-on was down while the event started and cleaned up: both rows are
    # still in the next snapshot and are notified in order on the next pass.
    restarted = link(entry(
        transition("accepted", "accepted"),
        transition("started", "started"),
        transition("cleanup", "cleanup_verified"),
    ))
    acknowledged: set[str] = set()
    await notify_export_transitions(settings(tmp_path), rest, restarted,  # type: ignore[arg-type]
                                    acknowledged=acknowledged,
                                    planned_for=lambda _: (None, None))
    # The durable dedupe store suppresses the already-sent accepted notice and
    # the two later transitions each go out exactly once.
    assert [call[2]["title"] for call in rest.calls] == [
        "Axle export accepted", "Axle export started", "Axle export cleanup verified",
    ]
    assert acknowledged == {"accepted", "started", "cleanup"}


async def test_no_notify_service_acknowledges_everything_unsent(tmp_path: Path) -> None:
    rest = FakeRest()
    acknowledged: set[str] = set()
    await notify_export_transitions(
        settings(tmp_path, service=""), rest,  # type: ignore[arg-type]
        link(entry(transition("a", "accepted"), transition("b", "armed"))),
        acknowledged=acknowledged, planned_for=lambda _: (None, None),
    )
    assert rest.calls == [] and acknowledged == {"a", "b"}


def test_summary_acknowledges_only_ids_still_in_the_snapshot(tmp_path: Path) -> None:
    state = AppState(Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db")),
                     tmp_path / "options.json")
    state.integration_link = link(entry(transition("a", "accepted"),
                                        transition("b", "started")))
    state.acknowledged_exports = {"a", "b", "gone"}
    assert state.export_acks() == ["a", "b"]


async def test_a_failed_send_stops_the_pass_so_the_loop_never_stalls(tmp_path: Path) -> None:
    rest = FakeRest(failures=1)
    acknowledged: set[str] = set()
    later = START + timedelta(days=1)
    body = link(entry(transition("a", "started"),
                      transition("b", "started", start=later, end=later + timedelta(hours=1))))
    await notify_export_transitions(settings(tmp_path), rest, body,  # type: ignore[arg-type]
                                    acknowledged=acknowledged,
                                    planned_for=lambda _: (None, None))
    # One failed call ends the pass: nothing after it is tried or acknowledged.
    assert acknowledged == set() and rest.calls == [] and rest.failures == 0


async def test_an_unknown_reason_is_never_echoed(tmp_path: Path) -> None:
    rest = FakeRest()
    acknowledged: set[str] = set()
    await notify_export_transitions(
        settings(tmp_path), rest,  # type: ignore[arg-type]
        link(entry(transition("a", "aborted", reason="alice_host"))),
        acknowledged=acknowledged, planned_for=lambda _: (None, None))
    [call] = rest.calls
    assert "alice_host" not in str(call[2]["message"])
    assert "operation refused" in str(call[2]["message"])
