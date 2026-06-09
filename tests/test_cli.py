"""Light CLI tests for the consumption import commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_spark.cli import _cmd_import_csv
from ha_spark.config import Settings

_CSV = (
    " Consumption (kwh), Start, End\n"
    "0.25,2026-06-01T00:00:00+01:00,2026-06-01T00:30:00+01:00\n"
    "0.50,2026-06-01T00:30:00+01:00,2026-06-01T01:00:00+01:00\n"
)


def test_import_csv_roundtrip_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_file = tmp_path / "export.csv"
    csv_file.write_text(_CSV, encoding="utf-8")
    settings = Settings(db_path=str(tmp_path / "test.db"))

    assert _cmd_import_csv(settings, [str(csv_file)]) == 0
    assert "Imported 2 intervals (2 new/updated)." in capsys.readouterr().out

    assert _cmd_import_csv(settings, [str(csv_file)]) == 0
    assert "Imported 2 intervals (0 new/updated)." in capsys.readouterr().out


def test_import_csv_missing_file_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    assert _cmd_import_csv(settings, [str(tmp_path / "nope.csv")]) == 2
    assert "Could not import" in capsys.readouterr().err
