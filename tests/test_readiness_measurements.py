"""Day-close measurements retain gaps rather than inventing real zeroes."""
from datetime import date, timedelta

import pytest

from ha_spark.config import Settings
from ha_spark.energy.readiness_ledger import ReadinessLedger, record_slot
from ha_spark.energy.readiness_measurements import (
    ReadinessDays,
    measurement_series,
    record_closed_days,
    snapshot_day,
)
from tests.test_readiness_ledger import START, revision


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch):
    """These tests pin cadence and retry, not the settle delay (tested on its own)."""
    monkeypatch.setattr("ha_spark.energy.readiness_measurements.SETTLE_DELAY", timedelta(0))



@pytest.mark.parametrize("unit,value", [("W", -1200), ("kW", -1.2), ("Wh", -100),
                                        ("kWh", -0.1)])
def test_five_minute_units_invert_and_partial_coverage(unit, value):
    rows = [{"start": (START + timedelta(minutes=5 * i)).timestamp() * 1000,
             "change": value, "mean": value} for i in range(6)]
    result = measurement_series(rows, [], unit, invert=True)
    assert result[START].kwh == pytest.approx(0.6)
    assert result[START].provenance == "measured"
    hourly = [{"start": START.timestamp() * 1000, "change": 10, "mean": 10}]
    assert measurement_series(rows[:-1], hourly, unit) == {}


async def test_day_close_retries_failure_then_preserves_snapshot(measured, monkeypatch, caplog):
    settings, _ = measured
    await seed(settings)
    from ha_spark.energy import readiness_measurements

    fetch = readiness_measurements.list_statistic_ids
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append("failed")
        raise RuntimeError("recorder unavailable")

    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", failed)
    await record_closed_days(settings, START + timedelta(days=1))
    assert await ReadinessDays(settings.db_path).days() == []
    assert "Readiness day-close recording failed" in caplog.text
    # Within the hour the failed day is left alone: no second fetch attempt.
    await record_closed_days(settings, START + timedelta(days=1, minutes=30))
    assert attempts == ["failed"]
    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", fetch)
    await record_closed_days(settings, START + timedelta(days=1, hours=1, minutes=1))
    (saved,) = await ReadinessDays(settings.db_path).days()
    assert saved.qualifying
    # A saved day is never re-attempted, even with the recorder failing again.
    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", failed)
    await record_closed_days(settings, START + timedelta(days=1, hours=2))
    assert await ReadinessDays(settings.db_path).days() == [saved]
    assert attempts == ["failed"]


async def test_expired_day_is_stored_excluded_even_when_recorder_unavailable(measured, monkeypatch):
    settings, _ = measured
    await seed(settings)

    async def forbidden(*args, **kwargs):
        raise RuntimeError("recorder unavailable")

    monkeypatch.setattr("ha_spark.energy.readiness_measurements.list_statistic_ids", forbidden)
    await record_closed_days(settings, START + timedelta(days=12))
    days = await ReadinessDays(settings.db_path).days()
    assert days[0].local_date == START.date()
    assert not days[0].qualifying
    assert any("retention" in reason for reason in days[0].reasons)


async def seed(settings):
    a = revision()
    for i in range(49):
        await record_slot(settings.db_path, START + timedelta(minutes=30 * i), a,
                          "observe", a.run.inputs.soc)
    # The next day's start is outside the original horizon.
    rows = await ReadinessLedger(settings.db_path).slots(START, START + timedelta(days=1))
    await ReadinessLedger(settings.db_path).record(rows[0].model_copy(update={
        "slot_start": START + timedelta(days=1),
    }))


@pytest.fixture
def measured(tmp_path, monkeypatch):
    settings = Settings(db_path=str(tmp_path / "ledger.db"), timezone="UTC", **{
        f"derive_{name}_entity": f"sensor.{name}" for name in (
            "grid_import", "grid_export", "solar_generation", "battery_charge", "battery_discharge"
        )
    })
    data = {}
    for name in ("grid_import", "grid_export", "solar_generation", "battery_charge",
                 "battery_discharge", "ev_charge"):
        data[f"sensor.{name}"] = [
            {"start": (START + timedelta(minutes=5 * i)).timestamp() * 1000,
             "change": 0.1 if name == "grid_import" else 0.0}
            for i in range(288)
        ]

    async def metadata(*args, **kwargs):
        return [{"statistic_id": entity, "unit_of_measurement": "kWh"} for entity in data]

    async def statistics(url, token, entity, start, **kwargs):
        return data[entity] if kwargs["period"] == "5minute" else []

    monkeypatch.setattr("ha_spark.energy.readiness_measurements.list_statistic_ids", metadata)
    monkeypatch.setattr(
        "ha_spark.energy.readiness_measurements.statistics_during_period", statistics,
    )
    return settings, data


async def test_true_zero_absent_and_substituted_zero_are_distinct(measured):
    settings, data = measured
    settings.derive_ev_charge_entity = "sensor.ev_charge"
    data["sensor.ev_charge"].pop(6)
    await seed(settings)
    day = await snapshot_day(settings, date(2026, 10, 20))
    assert day.slots[0].components["ev_charge"].provenance == "measured"
    assert day.slots[0].components["ev_charge"].kwh == 0
    assert day.slots[0].components["v2l"].provenance == "absent"
    assert day.slots[1].components["ev_charge"].provenance == "substituted"
    assert not day.qualifying
    assert any("ev_charge" in reason and "00:30" in reason for reason in day.reasons)


async def test_clean_day_snapshot_is_write_once(measured):
    settings, data = measured
    await seed(settings)
    first = await snapshot_day(settings, START.date())
    assert first.qualifying, first.reasons
    assert first.slots[0].load_kwh == pytest.approx(0.6)
    assert first.start_energy_kwh == first.end_energy_kwh == 5
    assert first.next_cheap_import_rate == 0.1
    data["sensor.grid_import"].clear()
    assert await snapshot_day(settings, START.date()) == first


async def test_hourly_only_spread_is_flagged(measured, monkeypatch):
    settings, data = measured
    data["sensor.grid_import"] = data["sensor.grid_import"][12:]

    async def statistics(url, token, entity, start, **kwargs):
        if kwargs["period"] == "5minute":
            return data[entity]
        return [{"start": START.timestamp() * 1000, "change": 2.0}] if (
            entity == "sensor.grid_import") else []

    monkeypatch.setattr(
        "ha_spark.energy.readiness_measurements.statistics_during_period", statistics,
    )
    await seed(settings)
    day = await snapshot_day(settings, START.date())
    assert day.qualifying, day.reasons
    for row in day.slots[:2]:
        assert row.components["grid_import"].kwh == 1
        assert row.components["grid_import"].provenance == "hourly_spread"


@pytest.mark.parametrize("problem", ["gap", "mode", "end_energy", "cheap", "clamped"])
async def test_day_exclusions_explain_every_gap(measured, problem):
    settings, data = measured
    await seed(settings)
    # Alter fixture evidence through a fresh ledger, not the immutable snapshot.
    import aiosqlite

    async with aiosqlite.connect(settings.db_path) as db:
        if problem == "gap":
            await db.execute(
                "DELETE FROM readiness_slots WHERE slot_start = ?", (START.isoformat(),),
            )
        else:
            rows = await ReadinessLedger(settings.db_path).slots(
                START, START + timedelta(days=2))
            for row in rows:
                updates = {}
                if problem == "mode" and row.slot_start == START:
                    updates = {"operating_mode": "protect"}
                if problem == "end_energy" and row.slot_start.date() != START.date():
                    updates = {"start_energy_kwh": None}
                if problem == "cheap":
                    updates = {"charge_window_fraction": 0}
                if updates:
                    await db.execute("UPDATE readiness_slots SET payload = ? WHERE slot_start = ?",
                                     (row.model_copy(update=updates).model_dump_json(),
                                      row.slot_start.isoformat()))
        await db.commit()
    if problem == "clamped":
        data["sensor.grid_export"][0]["change"] = 100
    day = await snapshot_day(settings, START.date())
    assert not day.qualifying
    expected = {"gap": "slot record missing", "mode": "not Observe", "end_energy": "end energy",
                "cheap": "next cheap", "clamped": "negative base load"}[problem]
    assert any(expected in reason for reason in day.reasons)


async def test_saved_days_leave_the_tick_idle(measured, monkeypatch):
    """A tick with nothing unsaved reads no slot rows and touches no recorder."""
    settings, _ = measured
    await seed(settings)
    from ha_spark.energy import readiness_measurements

    real_ledger = readiness_measurements.ReadinessLedger
    real_fetch = readiness_measurements.list_statistic_ids
    reads, fetches = [], []

    class CountingLedger:
        def __init__(self, db_path):
            self.inner = real_ledger(db_path)

        async def first_slot_start(self):
            return await self.inner.first_slot_start()

        async def slots(self, start, end):
            reads.append((start, end))
            return await self.inner.slots(start, end)

    async def counting_fetch(*args, **kwargs):
        fetches.append(args)
        return await real_fetch(*args, **kwargs)

    monkeypatch.setattr(readiness_measurements, "ReadinessLedger", CountingLedger)
    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", counting_fetch)
    await record_closed_days(settings, START + timedelta(days=1))
    # Only the attempted day's own slot range is read, never all history.
    assert reads == [(START, START + timedelta(days=3))]
    assert len(fetches) == 1
    await record_closed_days(settings, START + timedelta(days=1, minutes=5))
    assert reads == [(START, START + timedelta(days=3))]
    assert len(fetches) == 1


async def test_pending_next_day_evidence_waits_instead_of_excluding(measured):
    """A just-closed day whose cheap slot is still future stays unsaved."""
    settings, _ = measured
    await seed(settings)
    import aiosqlite

    # The next day's only recorded slot sits outside any charge window, so no
    # cheap slot exists yet for the closed day's end-of-day valuation.
    (boundary,) = await ReadinessLedger(settings.db_path).slots(
        START + timedelta(days=1), START + timedelta(days=1, minutes=30)
    )
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute(
            "UPDATE readiness_slots SET payload = ? WHERE slot_start = ?",
            (boundary.model_copy(update={"charge_window_fraction": 0.0}).model_dump_json(),
             (START + timedelta(days=1)).isoformat()),
        )
        await db.commit()
    await record_closed_days(settings, START + timedelta(days=1, minutes=5))
    assert await ReadinessDays(settings.db_path).days() == []
    # The cheap slot lands later in the next day; the retry then stores the day.
    await ReadinessLedger(settings.db_path).record(boundary.model_copy(update={
        "slot_start": START + timedelta(days=1, hours=2),
        "charge_window_fraction": 1.0,
    }))
    await record_closed_days(settings, START + timedelta(days=1, hours=2, minutes=10))
    (saved,) = await ReadinessDays(settings.db_path).days()
    assert saved.qualifying, saved.reasons


async def test_unsupported_unit_is_a_fetch_failure_never_a_substituted_zero(measured, monkeypatch):
    settings, _ = measured
    await seed(settings)
    from ha_spark.energy import readiness_measurements

    async def metadata(*args, **kwargs):
        return [{"statistic_id": "sensor.grid_import", "unit_of_measurement": "MWh"}]

    monkeypatch.setattr(readiness_measurements, "list_statistic_ids", metadata)
    with pytest.raises(ValueError, match="Unsupported unit"):
        await snapshot_day(settings, START.date())
    await record_closed_days(settings, START + timedelta(days=1))
    assert await ReadinessDays(settings.db_path).days() == []


async def test_raising_ledger_is_isolated_and_stores_nothing(measured, monkeypatch):
    settings, _ = measured
    await seed(settings)
    from ha_spark.energy import readiness_measurements

    class RaisingLedger:
        def __init__(self, db_path):
            self.db_path = db_path

        async def first_slot_start(self):
            raise RuntimeError("ledger unreadable")

        async def slots(self, start, end):
            raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(readiness_measurements, "ReadinessLedger", RaisingLedger)
    await record_closed_days(settings, START + timedelta(days=1))
    assert await ReadinessDays(settings.db_path).days() == []


async def test_day_close_waits_for_statistics_to_settle(measured, monkeypatch):
    settings, _ = measured
    await seed(settings)
    monkeypatch.setattr(
        "ha_spark.energy.readiness_measurements.SETTLE_DELAY", timedelta(hours=1)
    )
    await record_closed_days(settings, START + timedelta(days=1, minutes=5))
    assert await ReadinessDays(settings.db_path).days() == []
    await record_closed_days(settings, START + timedelta(days=1, hours=1, minutes=1))
    (saved,) = await ReadinessDays(settings.db_path).days()
    assert saved.qualifying
