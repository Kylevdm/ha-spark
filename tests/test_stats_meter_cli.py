"""CLI coverage for the stats reconciliation block and atomic meter CSV import."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy.stats import Reconciliation


def test_stats_prints_reconciliation_and_keeps_missing_store_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "household.db"
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings(db_path=str(db)))
    reconcile = Mock(return_value=Reconciliation({date(2026, 6, 1): -0.125}, -0.125, 1))
    monkeypatch.setattr(cli, "meter_reconciliation", reconcile)
    assert cli.main(["stats", "--range", "day", "--at", "2026-06-01"]) == 0
    output = capsys.readouterr().out
    assert "Meter reconciliation 2026-06-01: -12.5%" in output
    assert "Meter reconciliation for range: -12.5% (1/1 days covered)" in output
    assert not db.exists()


def test_malformed_csv_rejects_entire_import_without_writing_any_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "household.db"
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings(db_path=str(db)))
    file = tmp_path / "meter.csv"
    file.write_text(
        "Consumption (kWh),Start,End\n"
        "0.7,2026-06-01T00:00:00Z,2026-06-01T00:30:00Z\n"
        "sk_test,2026-06-01T00:30:00Z,2026-06-01T01:00:00Z\n"
    )
    assert cli.main(["import-csv", str(file)]) == 2
    assert not db.exists()
    assert capsys.readouterr().err == f"Could not import {file}: Unrecognized Octopus CSV row 3\n"
