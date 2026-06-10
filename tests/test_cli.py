"""Light CLI tests for the consumption import commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from ha_spark import cli
from ha_spark.cli import _cmd_backfill_load, _cmd_import_csv, _cmd_onboard, _cmd_run
from ha_spark.config import Settings
from ha_spark.health import CheckResult, Status

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


async def test_onboard_exit_code_tracks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.OK, "ready")

    async def warn(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.WARN, "thin")

    settings = Settings(ha_url="http://ha.test", ha_token="t")
    monkeypatch.setattr(cli, "check_load_history", ok)
    assert await _cmd_onboard(settings) == 0
    monkeypatch.setattr(cli, "check_load_history", warn)
    assert await _cmd_onboard(settings) == 2


async def test_backfill_load_requires_source(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    assert await _cmd_backfill_load(settings, source=None, list_only=False) == 2
    assert "--from" in capsys.readouterr().err


async def test_backfill_load_happy_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_backfill(_s: Settings, entity: str) -> tuple[int, str]:
        assert entity == "sensor.zappi"
        return 100, "2026-01-01 00:00 .. 2026-06-01 00:00 UTC"

    monkeypatch.setattr(cli, "backfill_load", fake_backfill)
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    assert await _cmd_backfill_load(settings, source="sensor.zappi", list_only=False) == 0
    out = capsys.readouterr().out
    assert "Imported 100 hourly stats" in out
    assert "ha_spark:house_load" in out


async def test_backfill_load_reports_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_backfill(_s: Settings, entity: str) -> tuple[int, str]:
        raise ValueError("no long-term statistics")

    monkeypatch.setattr(cli, "backfill_load", fake_backfill)
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    assert await _cmd_backfill_load(settings, source="sensor.x", list_only=False) == 2
    assert "Backfill failed" in capsys.readouterr().err


async def test_backfill_load_list_filters_supported_units(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_list(*args: object, **kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "statistic_id": "sensor.zappi",
                "statistics_unit_of_measurement": "W",
                "has_mean": True,
                "has_sum": False,
            },
            {
                "statistic_id": "sensor.temp",
                "statistics_unit_of_measurement": "°C",
                "has_mean": True,
                "has_sum": False,
            },
        ]

    monkeypatch.setattr(cli, "list_statistic_ids", fake_list)
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    assert await _cmd_backfill_load(settings, source=None, list_only=True) == 0
    out = capsys.readouterr().out
    assert "sensor.zappi" in out
    assert "sensor.temp" not in out
    assert "1 backfill-capable" in out


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
