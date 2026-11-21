"""The stats CLI identifies hourly backfill as lower resolution."""

from datetime import UTC, datetime

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot


def test_stats_labels_hourly_backfill_as_lower_resolution(monkeypatch, capsys):
    row = LedgerSlot(
        slot_start=datetime(2026, 1, 1, tzinfo=UTC),
        components={"grid_import": ComponentFlow(kwh=1, provenance="hourly_backfill")},
        house_load_kwh=1, base_load_kwh=1, exclusions=("hourly_backfill",),
    )

    async def slots(self, start, end):
        return [row]

    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: Settings(timezone="UTC"))
    monkeypatch.setattr(EnergyLedger, "slots", slots)
    assert cli.main(["stats", "--range", "day", "--at", "2026-01-01"]) == 0
    slot_line = capsys.readouterr().out.splitlines()[0]
    assert "lower resolution (hourly)" in slot_line
    assert "unpriced" in slot_line
