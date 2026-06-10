"""Light CLI tests for the consumption import commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_spark import cli
from ha_spark.cli import _cmd_import_csv, _cmd_run
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


async def test_run_once_invokes_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Settings] = []

    async def fake_run_once(s: Settings) -> None:
        calls.append(s)

    monkeypatch.setattr(cli, "run_once", fake_run_once)
    settings = Settings(ha_url="http://ha.test", ha_token="t")

    assert await _cmd_run(settings, once=True) == 0
    assert calls == [settings]


async def test_run_forever_handles_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_forever(_s: Settings) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_forever", fake_run_forever)
    settings = Settings(ha_url="http://ha.test", ha_token="t")

    assert await _cmd_run(settings, once=False) == 0
