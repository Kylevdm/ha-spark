"""The stats CLI renders the same figures as the pure statistics function."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from tests.test_stats import START


@pytest.mark.parametrize("at", [None, "2026-11-01"])
def test_stats_cli_prints_result(monkeypatch, capsys, at):
    class Clock:
        @staticmethod
        def now(tz):
            return START.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings(timezone="Europe/London"))
    rows = [LedgerSlot(
        slot_start=START, house_load_kwh=1.5, base_load_kwh=1.5,
        components={"grid_import": ComponentFlow(kwh=1.5, provenance="boundary")},
        import_session="free_electricity",
    )]
    reader = AsyncMock(return_value=rows)
    monkeypatch.setattr(EnergyLedger, "slots", reader)
    args = ["stats", "--range", "day"] + (["--at", at] if at else [])
    assert cli.main(args) == 0
    # One half-hour early: Happy Hour pricing context, never counted (#612).
    reader.assert_awaited_once_with(START - timedelta(minutes=30), START + timedelta(days=1))
    out = capsys.readouterr().out
    assert "grid_import=1.5 kWh (boundary)" in out
    assert "unpriced" in out
    assert "free_electricity" in out
    assert "2026-11-01T00:30+00:00 gap" in out
    assert "Totals: grid_import=1.5 kWh" in out
    assert "Coverage: 1/48" in out


def test_stats_cli_rejects_date_without_echo(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings())
    assert cli.main(["stats", "--range", "day", "--at", "invalid-date"]) == 2
    assert capsys.readouterr().err == "--at must be a valid YYYY-MM-DD date\n"


def test_stats_cli_store_error_is_sanitised(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_settings", lambda **_: Settings())
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(side_effect=OSError("private-path")))
    assert cli.main(["stats", "--range", "day", "--at", "2026-11-01"]) == 1
    assert capsys.readouterr().err == "Could not read energy ledger\n"
