"""Axle lifecycle facts through the recorder and its read-only history."""

import asyncio
from dataclasses import replace
from pathlib import Path

import respx

from ha_spark.api.snapshot_v4 import Event, Export, ExportTransition, SnapshotV4
from ha_spark.energy.axle_events import AxleEventRecorder, AxleEventStore
from ha_spark.energy.energy_ledger import EnergyLedger, RateEvidence
from ha_spark.energy.export_store import ExportEventStore
from ha_spark.energy.models import ExportIntent, FlexibilityEvent
from ha_spark.energy.readiness_measurements import HALF_HOUR
from ha_spark.energy.stats import axle_outcomes
from tests.test_energy_ledger import counter, settings_for
from tests.test_readiness_ledger import START, revision

END = START + 2 * HALF_HOUR
IDENTITY = f"export|{START.isoformat()}|{END.isoformat()}"
ACCEPTED = START - HALF_HOUR


def plan_revision(*, trusted=True, returned=None, issued=ACCEPTED, kw=(2, 4), matching=True):
    rev = revision(issued.isoformat())
    export = ExportIntent(
        ("export", START, END if matching else END + HALF_HOUR), START, END,
        2, 4, (START, START + HALF_HOUR), kw,
    )
    plan = replace(rev.run.plan, charge_intent=replace(rev.run.plan.charge_intent, export=export))
    inputs = replace(rev.run.inputs, flexibility_event=returned, flexibility_event_trusted=trusted)
    return replace(rev, issued_at=issued, run=replace(rev.run, plan=plan, inputs=inputs))


def snapshot(*kinds):
    rows = tuple(ExportTransition(
        id=f"row-{i}", kind=kind,
        event=Event(direction="export", start=START.isoformat(), end=END.isoformat()),
        at=(ACCEPTED if kind == "accepted" else START).isoformat(), reason=None,
    ) for i, kind in enumerate(kinds))
    return SnapshotV4(owner="integration", export=(Export(
        inverter_id="inverter", bound=None, supervision=None, record=None, stop_latch=None,
        transitions=rows,
    ),))


async def test_sources_record_the_same_acceptance_and_freeze_rate_and_commitment(tmp_path):
    records = []
    for native in (False, True):
        settings = settings_for(tmp_path).model_copy(update={
            "db_path": str(tmp_path / f"{native}.db"), "axle_event_rate_gbp_kwh": 1.75,
        })
        recorder = AxleEventRecorder()
        if not native:
            await ExportEventStore(settings.db_path).save(IDENTITY, END)
        for rate, rev in [(1.75, plan_revision()), (5.0, plan_revision(kw=(7, 8)))]:
            settings = settings.model_copy(update={"axle_event_rate_gbp_kwh": rate})
            integration = recorder.capture_native(snapshot("accepted") if native else None,
                                                  settings, rev)
            await recorder.record_tick(settings, ACCEPTED, rev, integration)
        [event] = await AxleEventStore(settings.db_path).events(START, END)
        assert (event.rate_gbp_kwh, event.committed_kwh, event.accepted_at) == (1.75, 3, ACCEPTED)
        records.append(event)
    assert records[0] == records[1]


async def test_tripped_is_preserved_and_only_a_later_trusted_read_cancels(tmp_path):
    settings = settings_for(tmp_path).model_copy(update={"export_event_provider": "axle"})
    recorder = AxleEventRecorder()
    rev = plan_revision(matching=False)
    assert recorder.capture_native(snapshot("accepted", "tripped", "aborted"), settings, rev)
    store = AxleEventStore(settings.db_path)
    recorder.capture_read(settings, plan_revision(issued=ACCEPTED))
    recorder.capture_read(settings, plan_revision(issued=START, trusted=False))
    await recorder.record_tick(settings, START, rev, True)
    [event] = await store.events(START, END)
    assert event.committed_kwh is None
    assert event.stop_reason == "tripped"
    assert event.cancelled_at is None

    still_active = FlexibilityEvent(START, END, "export", ACCEPTED, 1)
    recorder.capture_read(settings, plan_revision(issued=START, returned=still_active))
    recorder.capture_read(settings, plan_revision(issued=END))
    await recorder.record_tick(settings, END, rev, True)
    [event] = await store.events(START, END)
    assert event.cancelled_at is None

    recorder.capture_read(settings, plan_revision(issued=START + HALF_HOUR))
    await recorder.record_tick(settings, END, rev, True)
    [event] = await store.events(START, END)
    assert event.cancelled_at == START + HALF_HOUR
    assert await store.events(END, END + HALF_HOUR) == []


async def test_retries_failed_write_without_resampling_acceptance(tmp_path, caplog):
    settings = settings_for(tmp_path)
    db_path = Path(settings.db_path)
    await asyncio.to_thread(db_path.mkdir)  # The next tick can open SQLite again.
    recorder = AxleEventRecorder()
    rev = plan_revision()
    recorder.capture_native(snapshot("accepted", "tripped"), settings, rev)
    await recorder.record_tick(settings, START, rev, True)
    assert [rec.message for rec in caplog.records] == ["Axle event recording failed"]
    await asyncio.to_thread(db_path.rmdir)

    reloaded = settings.model_copy(update={"axle_event_rate_gbp_kwh": 5.0})
    rev = plan_revision(kw=(7, 8))
    recorder.capture_native(snapshot(), reloaded, rev)  # The notice queue has acked the rows.
    await recorder.record_tick(reloaded, START, rev, True)
    [event] = await AxleEventStore(settings.db_path).events(START, END)
    assert (event.rate_gbp_kwh, event.committed_kwh, event.stop_reason) == (1, 3, "tripped")


async def test_trusted_read_can_cancel_before_the_native_notice_arrives(tmp_path):
    settings = settings_for(tmp_path).model_copy(update={"export_event_provider": "axle"})
    recorder = AxleEventRecorder()
    rev = plan_revision(issued=START)
    recorder.capture_read(settings, rev)
    await recorder.record_tick(settings, START, rev, True)
    recorder.capture_native(snapshot("accepted"), settings, rev)
    await recorder.record_tick(settings, START + HALF_HOUR, rev, True)
    [event] = await AxleEventStore(settings.db_path).events(START, END)
    assert event.cancelled_at == START


@respx.mock
async def test_tripped_record_uses_measured_delivery_and_needs_the_whole_window(tmp_path):
    settings = settings_for(tmp_path, derive_grid_export_entity="sensor.grid_export")
    recorder = AxleEventRecorder()
    rev = plan_revision()
    recorder.capture_native(snapshot("accepted", "tripped"), settings, rev)
    await recorder.record_tick(settings, START, rev, True)
    events = await AxleEventStore(settings.db_path).events(START, END)

    middle = START + HALF_HOUR
    counter("sensor.grid_import", ("10", START), ("11", middle), ("12", END))
    counter("sensor.grid_export", ("10", START), ("11", middle), ("11", END))
    ledger = EnergyLedger(settings.db_path)
    for tick in (START, middle):
        await ledger.record_boundary(settings, tick, RateEvidence())
    [gap] = axle_outcomes(events, await ledger.slots(START, END)).events
    assert gap.outcome == "unknown"
    assert gap.earnings_gbp is None

    await ledger.record_boundary(settings, END, RateEvidence())
    [scored] = axle_outcomes(events, await ledger.slots(START, END)).events
    assert scored.event.stop_reason == "tripped"
    assert (scored.outcome, scored.delivered_kwh, scored.earnings_gbp) == ("partial", 1, 1)


async def test_missing_store_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert await AxleEventStore(str(path)).events(START, END) == []
    assert not path.exists()
