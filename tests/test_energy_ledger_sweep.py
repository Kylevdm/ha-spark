"""The daily sweep re-checks stored days against the plausibility rule."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from ha_spark.energy import energy_ledger
from ha_spark.energy.energy_ledger import (
    ComponentFlow,
    EnergyLedger,
    LedgerSlot,
    RateEvidence,
    sweep_closed_days,
)
from ha_spark.energy.readiness_measurements import COMPONENTS
from tests.test_energy_ledger import settings_for
from tests.test_energy_ledger_gaps import fake_recorder, five_minutes

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=ZoneInfo("Europe/London"))
RATES = RateEvidence(import_rate=0.2, export_rate=0.05, revision_id="A")
GAPS = {name: ComponentFlow(reason="not_configured") for name in COMPONENTS}


def slot(at: datetime, kwh: float, provenance: str = "boundary", **extra) -> LedgerSlot:
    components = dict(GAPS)
    components["grid_import"] = ComponentFlow(kwh=kwh, provenance=provenance)
    rates = RATES.model_dump() | extra
    return LedgerSlot(
        **rates, slot_start=at, components=components,
        house_load_kwh=kwh, base_load_kwh=kwh,
    )


def key(at: datetime) -> str:
    return at.astimezone(UTC).isoformat()


async def seed(path: str, *rows: LedgerSlot) -> None:
    # History as an older release wrote it: direct rows, no write-time rule.
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS energy_ledger_slots ("
            "slot_start TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        await db.executemany(
            "INSERT INTO energy_ledger_slots VALUES (?, ?)",
            [(key(row.slot_start), row.model_dump_json()) for row in rows],
        )
        await db.commit()


async def payloads(path: str) -> dict[str, str]:
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT slot_start, payload FROM energy_ledger_slots") as cursor:
            return dict(await cursor.fetchall())


async def test_sweep_retires_stored_spikes_but_not_plausible_history(tmp_path, caplog):
    settings = settings_for(tmp_path, timezone="Europe/London")
    incident = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)
    spread_spike = datetime(2026, 11, 20, 4, 0, tzinfo=UTC)
    stale = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)  # Nine days back: outside the sweep.
    normal = [slot(datetime(2026, 11, day, 12, 0, tzinfo=UTC), 2) for day in range(15, 22)]
    await seed(settings.db_path, *normal, slot(incident, 27800), slot(stale, 27800),
               slot(spread_spike, 27810, provenance="hourly_backfill",
                    exclusions=("hourly_backfill",)))
    before = await payloads(settings.db_path)

    await sweep_closed_days(settings, NOW)

    after = await payloads(settings.db_path)
    ledger = EnergyLedger(settings.db_path)
    rows = {row.slot_start: row for row in await ledger.slots(
        stale - timedelta(days=1), NOW)}
    for at in (incident, spread_spike):
        flow = rows[at].components["grid_import"]
        assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")
        assert rows[at].house_load_kwh is rows[at].base_load_kwh is None
        assert (rows[at].import_rate, rows[at].export_rate, rows[at].revision_id) == (
            0.2, 0.05, "A")
    assert rows[spread_spike].exclusions == ("hourly_backfill",)
    # Passing rows and out-of-window history keep their byte-identical payload.
    for row in (*normal, slot(stale, 27800)):
        assert after[key(row.slot_start)] == before[key(row.slot_start)]
    assert after[key(incident)] != before[key(incident)]
    assert caplog.messages == ["Energy ledger sweep rejected 2 implausible intervals"]
    assert "27800" not in caplog.text and "27810" not in caplog.text
    assert "sensor" not in caplog.text

    # The next day's sweep is a no-op: the stored gaps already pass the rule.
    await sweep_closed_days(settings, NOW + timedelta(days=1))
    assert await payloads(settings.db_path) == after
    assert caplog.messages == ["Energy ledger sweep rejected 2 implausible intervals"]


async def test_a_swept_day_is_refilled_despite_its_done_marker(tmp_path, monkeypatch):
    # The incident day was filled long before the sweep existed: its done
    # marker must be cleared so the same background run re-fills the new gap.
    settings = settings_for(tmp_path, timezone="UTC")
    day_start = datetime(2026, 11, 19, 0, 0, tzinfo=UTC)
    spiked = day_start + timedelta(minutes=30)
    now = datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
    await seed(settings.db_path, slot(spiked, 27800))
    energy_ledger._fill_attempts[(settings.db_path, day_start)] = None
    fake_recorder(
        monkeypatch, {"sensor.grid_import": "kWh"},
        lambda entity, start: five_minutes(spiked, change="0.5") if start == day_start else [],
    )

    await energy_ledger.fill_closed_days(settings, now)

    [row] = await EnergyLedger(settings.db_path).slots(day_start, day_start + timedelta(days=1))
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance) == (3, "statistics_fill")
    assert row.house_load_kwh == row.base_load_kwh == 3
    assert (row.import_rate, row.export_rate, row.revision_id) == (0.2, 0.05, "A")


async def test_sweep_of_a_missing_ledger_never_creates_it(tmp_path):
    settings = settings_for(tmp_path)
    await sweep_closed_days(settings, NOW)
    assert not (tmp_path / "ledger.db").exists()


async def test_sweep_failure_is_contained_and_retries(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    incident = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)
    await seed(settings.db_path, slot(incident, 27800))

    def fail(*args, **kwargs):
        raise OSError("private storage detail")

    with monkeypatch.context() as patch:
        patch.setattr(energy_ledger.aiosqlite, "connect", fail)
        await sweep_closed_days(settings, NOW)
    assert caplog.messages == ["Energy ledger sweep failed"]
    assert "private storage detail" not in caplog.text
    # The day is not marked swept, so the next background run retries it.
    await sweep_closed_days(settings, NOW + timedelta(minutes=30))
    [row] = await EnergyLedger(settings.db_path).slots(incident, incident + timedelta(minutes=30))
    flow = row.components["grid_import"]
    assert (flow.kwh, flow.provenance, flow.reason) == (None, "absent", "rejected")
