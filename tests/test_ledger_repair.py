"""Repair-data: stored spikes become rejected gaps and refill from HA statistics."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import energy_ledger, ledger_repair
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.ledger_repair import claim_repair, last_repair, run_ledger_repair
from ha_spark.energy.readiness_measurements import COMPONENTS
from tests.test_energy_ledger import counter

HALF = timedelta(minutes=30)
# A closed day well inside the recorder's ten-day 5-minute retention.
DAY = datetime(2026, 1, 10, tzinfo=UTC)
NOW = DAY + timedelta(days=2, hours=12)
# A closed day beyond retention, served by hourly long-term statistics only.
OLD_DAY = datetime(2026, 1, 1, tzinfo=UTC)
OLD_NOW = datetime(2026, 1, 20, 12, tzinfo=UTC)


def settings_for(tmp_path, **kwargs):
    return Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ledger.db"),
        timezone="UTC", derive_grid_import_entity="sensor.grid_import", **kwargs,
    )


async def store_slots(settings, *slots):
    """Seed stored ledger rows the way the recorder and backfill leave them."""
    async with aiosqlite.connect(settings.db_path) as db:
        await db.executescript(energy_ledger._SCHEMA)
        for start, components in slots:
            # Real rows always carry every component, configured or not.
            flows = {name: components.get(name, ComponentFlow(reason="not_configured"))
                     for name in COMPONENTS}
            row = LedgerSlot(slot_start=start, components=flows,
                             exclusions=("no_rate_capture",))
            await db.execute(
                "INSERT INTO energy_ledger_slots VALUES (?, ?)",
                (start.astimezone(UTC).isoformat(), row.model_dump_json()),
            )
        await db.commit()


async def repair(settings, now):
    repair_id = await claim_repair(settings.db_path, now)
    assert repair_id is not None, "a fresh claim must succeed"
    return await run_ledger_repair(settings, repair_id, now, finished_at=now)


def five_minutes(start, *, count=6, **values):
    return [
        {"start": (start + timedelta(minutes=5 * i)).timestamp() * 1000, **values}
        for i in range(count)
    ]


def fake_recorder(monkeypatch, units, read):
    async def metadata(*args, **kwargs):
        return [
            {"statistic_id": entity, "statistics_unit_of_measurement": unit}
            for entity, unit in units.items()
        ]

    async def statistics(url, token, entity, start, *, period, **kwargs):
        assert period == "5minute"
        return read(entity, start)

    monkeypatch.setattr(energy_ledger, "list_statistic_ids", metadata)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", statistics)


async def test_a_stored_spike_is_rejected_then_refilled_and_passing_values_untouched(
    tmp_path, monkeypatch, caplog,
):
    settings = settings_for(tmp_path)
    await store_slots(
        settings,
        (DAY, {"grid_import": ComponentFlow(kwh=27800.0, provenance="boundary")}),
        (DAY + HALF, {"grid_import": ComponentFlow(kwh=2.0, provenance="boundary")}),
    )
    # The recorder now holds plausible 5-minute rows for both slots.
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: [
            row for slot in (DAY, DAY + HALF) for row in five_minutes(slot, change="1")
        ] if start == DAY else [],
    )
    run = await repair(settings, NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 1, 0)
    first, second = await EnergyLedger(settings.db_path).slots(DAY, DAY + 2 * HALF)
    assert (first.components["grid_import"].kwh,
            first.components["grid_import"].provenance) == (6, "statistics_fill")
    assert first.house_load_kwh == first.base_load_kwh == 6
    assert (second.components["grid_import"].kwh,
            second.components["grid_import"].provenance) == (2.0, "boundary")
    stored = await last_repair(settings.db_path)
    assert (stored.status, stored.checked_days, stored.fixed_slots,
            stored.still_gaps) == ("done", 1, 1, 0)
    assert "Statistics repair rejected an implausible interval" in caplog.messages
    assert "27800" not in caplog.text


async def test_a_failed_statistics_read_leaves_the_rejected_gap_counted(
    tmp_path, monkeypatch, caplog,
):
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=27800.0, provenance="hourly_backfill"),
    }))

    async def broken(*args, **kwargs):
        raise RuntimeError("private recorder payload")

    monkeypatch.setattr(energy_ledger, "list_statistic_ids", broken)
    run = await repair(settings, NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 0, 1)
    [row] = await EnergyLedger(settings.db_path).slots(DAY, DAY + HALF)
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")
    assert row.house_load_kwh is row.base_load_kwh is None
    assert caplog.messages.count("Statistics repair fill failed") == 1
    assert "private recorder payload" not in caplog.text


async def test_hourly_refill_beyond_retention_fills_gaps_only(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    await store_slots(
        settings,
        (OLD_DAY, {"grid_import": ComponentFlow(reason="missed_boundary")}),
        (OLD_DAY + HALF, {"grid_import": ComponentFlow(kwh=1.0, provenance="hourly_backfill")}),
    )

    async def metadata(*args, **kwargs):
        return [{"statistic_id": "sensor.grid_import",
                 "statistics_unit_of_measurement": "kWh"}]

    async def statistics(url, token, entity, start, *, period, end_time, **kwargs):
        assert period == "hour"
        return [
            {"start": (OLD_DAY + timedelta(hours=i)).timestamp() * 1000, "change": 2}
            for i in range(2)
        ]

    monkeypatch.setattr(ledger_repair, "list_statistic_ids", metadata)
    monkeypatch.setattr(ledger_repair, "statistics_during_period", statistics)
    run = await repair(settings, OLD_NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 1, 0)
    first, second = await EnergyLedger(settings.db_path).slots(OLD_DAY, OLD_DAY + 2 * HALF)
    assert (first.components["grid_import"].kwh,
            first.components["grid_import"].provenance) == (pytest.approx(1.0), "hourly_backfill")
    assert first.house_load_kwh == pytest.approx(1)
    assert first.base_load_kwh == pytest.approx(1)
    # A passing value is never overwritten, and no row is created for absent slots.
    assert (second.components["grid_import"].kwh,
            second.components["grid_import"].provenance) == (pytest.approx(1.0), "hourly_backfill")
    assert await EnergyLedger(settings.db_path).slots(OLD_DAY + 2 * HALF, OLD_DAY + 4 * HALF) == []


async def test_second_claim_is_refused_while_one_runs(tmp_path):
    settings = settings_for(tmp_path)
    first = await claim_repair(settings.db_path, NOW)
    assert first is not None
    at = NOW + timedelta(minutes=1)
    assert await claim_repair(settings.db_path, at) is None
    assert (await last_repair(settings.db_path, now=at)).status == "running"


async def test_a_stale_running_claim_counts_as_failed_and_is_reclaimable(tmp_path):
    settings = settings_for(tmp_path)
    stale = await claim_repair(settings.db_path, NOW - timedelta(hours=7))
    assert stale is not None
    fresh = await claim_repair(settings.db_path, NOW)
    assert fresh is not None and fresh != stale
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute(
            "SELECT status FROM energy_ledger_repairs WHERE id = ?", (stale,),
        ) as cursor:
            assert await cursor.fetchone() == ("failed",)


async def test_a_stale_running_claim_reads_as_failed_until_reclaimed(tmp_path):
    # A crashed job's claim is presented as failed, so the page stops polling,
    # the button re-enables, and the owner can make the POST that reclaims it.
    settings = settings_for(tmp_path)
    stale = await claim_repair(settings.db_path, NOW - timedelta(hours=7))
    assert stale is not None
    run = await last_repair(settings.db_path, now=NOW)
    assert run is not None and (run.status, run.finished_at) == ("failed", None)
    # The presentation is read-only: the row itself waits for the next claim.
    async with aiosqlite.connect(settings.db_path) as db:
        async with db.execute("SELECT status FROM energy_ledger_repairs") as cursor:
            assert await cursor.fetchone() == ("running",)


async def test_a_concurrent_correction_wins_over_a_stale_rejection(tmp_path, monkeypatch, caplog):
    # Another writer replaces the spike after the repair's scan but before its
    # write transaction: the current value is re-checked inside the
    # transaction, and the corrected reading is never turned into a gap.
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=27800.0, provenance="boundary"),
    }))
    stale = await EnergyLedger(settings.db_path).slots(DAY, DAY + HALF)
    original = EnergyLedger.slots
    reads = 0

    async def concurrent(self, start, end):
        nonlocal reads
        reads += 1
        if reads == 1:
            corrected = LedgerSlot(
                slot_start=DAY, exclusions=("no_rate_capture",),
                components={name: ComponentFlow(reason="not_configured") for name in COMPONENTS},
            ).model_copy(update={"components": {
                **{name: ComponentFlow(reason="not_configured") for name in COMPONENTS},
                "grid_import": ComponentFlow(kwh=2.0, provenance="boundary"),
            }})
            async with aiosqlite.connect(settings.db_path) as db:
                await db.execute(
                    "INSERT INTO energy_ledger_slots VALUES (?, ?) ON CONFLICT(slot_start) "
                    "DO UPDATE SET payload = excluded.payload",
                    (DAY.isoformat(), corrected.model_dump_json()),
                )
                await db.commit()
            return stale
        return await original(self, start, end)

    monkeypatch.setattr(EnergyLedger, "slots", concurrent)
    run = await repair(settings, NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 0, 0)
    [row] = await original(EnergyLedger(settings.db_path), DAY, DAY + HALF)
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (2.0, "boundary", None)
    assert "Statistics repair rejected an implausible interval" not in caplog.messages


async def test_a_removed_sensor_s_stored_spike_is_still_rejected(tmp_path, caplog):
    # The solar sensor was removed after a spike was recorded: its stored flow
    # is re-checked regardless, while configuration alone decides what can be
    # re-filled and what counts toward the remaining gaps.
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=1.0, provenance="boundary"),
        "solar_generation": ComponentFlow(kwh=27800.0, provenance="boundary"),
    }))
    run = await repair(settings, NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 0, 0)
    [row] = await EnergyLedger(settings.db_path).slots(DAY, DAY + HALF)
    assert row.components["solar_generation"].reason == "rejected"
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance) == (1.0, "boundary")
    # A rejected component is a real gap: the derived loads step back to None.
    assert row.house_load_kwh is row.base_load_kwh is None
    assert "Statistics repair rejected an implausible interval" in caplog.messages


async def test_a_reclaimed_worker_writes_nothing_and_cannot_flip_its_failed_row(
    tmp_path, monkeypatch, caplog,
):
    # A slow worker resumes after another process reclaimed its six-hour-old
    # claim: every ledger write is fenced against the active claim, and its
    # finalization cannot turn the failed row back into done.
    settings = settings_for(tmp_path)
    await store_slots(
        settings,
        (DAY, {"grid_import": ComponentFlow(kwh=27800.0, provenance="boundary")}),
        (DAY + HALF, {"grid_import": ComponentFlow(reason="missed_boundary")}),
    )
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: [
            row for slot in (DAY, DAY + HALF) for row in five_minutes(slot, change="1")
        ] if start == DAY else [],
    )
    stale = await claim_repair(settings.db_path, NOW)
    fresh = await claim_repair(settings.db_path, NOW + timedelta(hours=7))
    assert stale is not None and fresh is not None and fresh != stale
    run = await run_ledger_repair(settings, stale, NOW, finished_at=NOW)
    assert run.status == "failed"
    assert "Statistics repair was superseded" in caplog.messages
    first, second = await EnergyLedger(settings.db_path).slots(DAY, DAY + 2 * HALF)
    assert (first.components["grid_import"].kwh,
            first.components["grid_import"].provenance) == (27800.0, "boundary")
    assert second.components["grid_import"].reason == "missed_boundary"
    async with aiosqlite.connect(settings.db_path) as db:
        statuses = {}
        async with db.execute("SELECT id, status FROM energy_ledger_repairs") as cursor:
            async for key, status in cursor:
                statuses[int(key)] = status
    assert statuses[stale] == "failed" and statuses[fresh] == "running"


async def test_old_days_without_gaps_are_never_read(tmp_path, monkeypatch):
    # A complete old ledger is validated without a single HA read: ranges
    # without gaps are skipped, so an hourly sweep over the whole history
    # never happens with nothing to fix.
    settings = settings_for(tmp_path)
    await store_slots(
        settings,
        (OLD_DAY, {"grid_import": ComponentFlow(kwh=1.0, provenance="hourly_backfill")}),
        (OLD_DAY + timedelta(days=2),
         {"grid_import": ComponentFlow(kwh=1.0, provenance="hourly_backfill")}),
    )

    async def unexpected(*args, **kwargs):
        pytest.fail("repair read HA for a ledger without gaps")

    monkeypatch.setattr(ledger_repair, "list_statistic_ids", unexpected)
    monkeypatch.setattr(ledger_repair, "statistics_during_period", unexpected)
    monkeypatch.setattr(energy_ledger, "list_statistic_ids", unexpected)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", unexpected)
    run = await repair(settings, OLD_NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 2, 0, 0)


async def test_hourly_reads_stay_bounded_to_contiguous_gap_ranges(tmp_path, monkeypatch):
    # Two gap days three days apart are read as two disjoint bounded ranges,
    # not one sweep from the first to the last.
    settings = settings_for(tmp_path)
    other = OLD_DAY + timedelta(days=3)
    await store_slots(
        settings,
        (OLD_DAY, {"grid_import": ComponentFlow(reason="missed_boundary")}),
        (other, {"grid_import": ComponentFlow(reason="missed_boundary")}),
    )
    read = []

    async def metadata(*args, **kwargs):
        return [{"statistic_id": "sensor.grid_import",
                 "statistics_unit_of_measurement": "kWh"}]

    async def statistics(url, token, entity, start, *, period, end_time, **kwargs):
        assert period == "hour"
        read.append((start, end_time))
        return []

    monkeypatch.setattr(ledger_repair, "list_statistic_ids", metadata)
    monkeypatch.setattr(ledger_repair, "statistics_during_period", statistics)
    monkeypatch.setattr(energy_ledger, "list_statistic_ids", metadata)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", statistics)
    run = await repair(settings, OLD_NOW)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 2, 0, 2)
    assert read == [
        (OLD_DAY, OLD_DAY + timedelta(days=1)),
        (other, other + timedelta(days=1)),
    ]


async def test_the_retention_cutoff_day_is_served_per_slot(tmp_path, monkeypatch):
    # The ten-day retention edge falls at noon on a stored day: the evening
    # gap is filled from the six retained five-minute readings, the morning
    # gap from hourly statistics, instead of discarding the day whole.
    settings = settings_for(tmp_path)
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    day = datetime(2026, 1, 5, tzinfo=UTC)
    noon = datetime(2026, 1, 5, 12, tzinfo=UTC)
    morning, evening = day + timedelta(hours=6), day + timedelta(hours=18)
    await store_slots(
        settings,
        (morning, {"grid_import": ComponentFlow(reason="missed_boundary")}),
        (evening, {"grid_import": ComponentFlow(reason="missed_boundary")}),
    )

    async def metadata(*args, **kwargs):
        return [{"statistic_id": "sensor.grid_import",
                 "statistics_unit_of_measurement": "kWh"}]

    async def statistics(url, token, entity, start, *, period, end_time, **kwargs):
        if period == "hour":
            assert (start, end_time) == (day, noon)
            return [{"start": morning.timestamp() * 1000, "change": 2}]
        assert period == "5minute" and start == noon
        return five_minutes(evening, change="1")

    monkeypatch.setattr(ledger_repair, "list_statistic_ids", metadata)
    monkeypatch.setattr(ledger_repair, "statistics_during_period", statistics)
    monkeypatch.setattr(energy_ledger, "list_statistic_ids", metadata)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", statistics)
    run = await repair(settings, now)
    assert (run.status, run.checked_days, run.fixed_slots, run.still_gaps) == ("done", 1, 2, 0)
    rows = {row.slot_start: row for row in
            await EnergyLedger(settings.db_path).slots(morning, day + timedelta(days=1))}
    assert (rows[morning].components["grid_import"].kwh,
            rows[morning].components["grid_import"].provenance) == (
                pytest.approx(1.0), "hourly_backfill")
    assert (rows[evening].components["grid_import"].kwh,
            rows[evening].components["grid_import"].provenance) == (6, "statistics_fill")


async def test_a_storage_failure_fails_the_run(tmp_path, caplog):
    # A database write failure inside a refill is a failed run, never a
    # counted gap: only an unavailable HA source degrades to done.
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(reason="missed_boundary"),
    }))
    repair_id = await claim_repair(settings.db_path, NOW)
    await asyncio.to_thread(Path(settings.db_path).chmod, 0o444)
    try:
        run = await run_ledger_repair(settings, repair_id, NOW, finished_at=NOW)
    finally:
        await asyncio.to_thread(Path(settings.db_path).chmod, 0o644)
    assert run.status == "failed"
    assert caplog.messages.count("Statistics repair failed") >= 1
    assert "readonly" not in caplog.text


async def test_a_finalization_failure_reports_a_failed_run(tmp_path, monkeypatch, caplog):
    # If the run record cannot be persisted, the outcome is never reported
    # as done while the database still says running.
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=2.0, provenance="boundary"),
    }))

    async def broken(*args, **kwargs):
        raise sqlite3.OperationalError("private storage detail")

    monkeypatch.setattr(ledger_repair, "_finish_repair", broken)
    repair_id = await claim_repair(settings.db_path, NOW)
    run = await run_ledger_repair(settings, repair_id, NOW, finished_at=NOW)
    assert run.status == "failed"
    assert "Statistics repair was superseded" in caplog.messages
    assert (await last_repair(settings.db_path, now=NOW)).status == "running"
    assert "private storage detail" not in caplog.text


async def test_the_completion_time_is_captured_after_the_pass(tmp_path):
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=2.0, provenance="boundary"),
    }))
    repair_id = await claim_repair(settings.db_path, NOW)
    done = NOW + timedelta(minutes=20)
    run = await run_ledger_repair(settings, repair_id, NOW, finished_at=done)
    assert (run.started_at, run.finished_at) == (NOW, done)
    stored = await last_repair(settings.db_path)
    assert stored is not None
    assert (stored.started_at, stored.finished_at, stored.status) == (NOW, done, "done")


async def test_nothing_configured_or_stored_reads_nothing(tmp_path, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("repair read HA with nothing to check")

    monkeypatch.setattr(ledger_repair, "list_statistic_ids", unexpected)
    monkeypatch.setattr(ledger_repair, "statistics_during_period", unexpected)
    monkeypatch.setattr(energy_ledger, "list_statistic_ids", unexpected)
    monkeypatch.setattr(energy_ledger, "statistics_during_period", unexpected)
    empty = Settings(db_path=str(tmp_path / "empty.db"), timezone="UTC")
    assert (await repair(empty, NOW)).model_dump(exclude={"started_at", "finished_at"}) == {
        "status": "done", "checked_days": 0, "fixed_slots": 0, "still_gaps": 0,
    }
    unconfigured = Settings(db_path=str(tmp_path / "rows.db"), timezone="UTC")
    await store_slots(unconfigured, (DAY, {
        "grid_import": ComponentFlow(kwh=27800.0, provenance="boundary"),
    }))
    run = await repair(unconfigured, NOW)
    # With every sensor removed nothing can be re-filled, but the stored day is
    # still checked: the spike becomes a rejected gap, and no configured
    # component remains to count as a still-open gap.
    assert (run.checked_days, run.fixed_slots, run.still_gaps) == (1, 0, 0)
    [row] = await EnergyLedger(unconfigured.db_path).slots(DAY, DAY + HALF)
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")


async def test_an_unexpected_error_fails_the_run_without_payload(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=2.0, provenance="boundary"),
    }))

    async def broken(settings, now, repair_id):
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(ledger_repair, "_repair_history", broken)
    repair_id = await claim_repair(settings.db_path, NOW)
    run = await run_ledger_repair(settings, repair_id, NOW)
    assert run.status == "failed"
    assert caplog.messages == ["Statistics repair failed"]
    assert "private failure detail" not in caplog.text
    assert (await last_repair(settings.db_path)).status == "failed"


def test_repair_module_has_no_write_path():
    from tests.test_energy_ledger import test_statistics_modules_have_no_write_path

    test_statistics_modules_have_no_write_path("ledger_repair")


@respx.mock
async def test_a_boundary_capture_interleaves_with_the_repair(tmp_path, monkeypatch):
    # The repair's write transactions are short, so a concurrent boundary
    # capture commits alongside it instead of blocking a protection tick.
    settings = settings_for(tmp_path)
    await store_slots(settings, (DAY, {
        "grid_import": ComponentFlow(kwh=27800.0, provenance="boundary"),
    }))
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: five_minutes(DAY, change="1") if start == DAY else [],
    )
    # No previous reading: the capture still commits its own slot row.
    counter("sensor.grid_import", ("10", DAY + 2 * HALF))
    ledger = EnergyLedger(settings.db_path)
    repair_id = await claim_repair(settings.db_path, NOW)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run_ledger_repair(settings, repair_id, NOW, finished_at=NOW))
        tasks.create_task(ledger.record_boundary(
            settings, DAY + 2 * HALF, energy_ledger.RateEvidence(import_rate=0.2),
        ))
    first, captured = await ledger.slots(DAY, DAY + 3 * HALF)
    assert first.components["grid_import"].provenance == "statistics_fill"
    assert captured.components["grid_import"].reason == "no_previous_reading"
