"""Coverage and provenance reported by the pure statistics function."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy import stats
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.stats import compute_stats


def test_statistics_fill_counts_toward_energy_coverage_and_provenance_totals():
    start = datetime(2026, 10, 20, tzinfo=UTC)
    step = timedelta(minutes=30)
    rows = [
        LedgerSlot(
            slot_start=start + step * i,
            components={"grid_import": ComponentFlow(kwh=kwh, provenance=provenance)},
            house_load_kwh=kwh, base_load_kwh=kwh,
        )
        for i, (provenance, kwh) in enumerate([
            ("statistics_fill", 2), ("distributed", 1), ("absent", None), ("boundary", 3),
        ])
    ]
    result = compute_stats(rows, start, start + 5 * step, UTC)
    assert result.energy_coverage == 3 / 5
    assert result.totals.components_kwh["grid_import"] == 6
    counts = stats.provenance_counts(result)
    assert counts == {"statistics_fill": 1, "distributed": 1, "absent": 1, "boundary": 1, "gap": 1}


def test_rejected_gaps_count_by_their_own_reason():
    start = datetime(2026, 11, 19, tzinfo=UTC)
    step = timedelta(minutes=30)
    rows = [
        LedgerSlot(slot_start=start, components={
            "grid_import": ComponentFlow(reason="rejected"),
        }),
        LedgerSlot(slot_start=start + step, components={
            "grid_import": ComponentFlow(kwh=1, provenance="boundary"),
        }),
    ]
    result = compute_stats(rows, start, start + 2 * step, UTC)
    assert stats.provenance_counts(result) == {"rejected": 1, "boundary": 1}


def test_stats_cli_counts_a_rejected_gap_among_gaps(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings(timezone="UTC"))
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[LedgerSlot(
        slot_start=datetime(2026, 10, 20, tzinfo=UTC),
        components={"grid_import": ComponentFlow(reason="rejected")},
    )]))
    assert cli.main(["stats", "--range", "day", "--at", "2026-10-20"]) == 0
    assert "gaps 48" in capsys.readouterr().out


def test_stats_cli_renders_provenance_counts(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings(timezone="UTC"))
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[LedgerSlot(
        slot_start=datetime(2026, 10, 20, tzinfo=UTC),
        components={"grid_import": ComponentFlow(kwh=2, provenance="statistics_fill")},
        house_load_kwh=2, base_load_kwh=2,
    )]))
    assert cli.main(["stats", "--range", "day", "--at", "2026-10-20"]) == 0
    assert "filled 1 (statistics_fill), distributed 0, gaps 47" in capsys.readouterr().out
