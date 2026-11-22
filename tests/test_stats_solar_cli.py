"""The stats CLI renders the solar accuracy result and its coverage."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.evening_forecast import EveningForecastStore, SolarForecastSlot


def test_stats_cli_renders_evening_plan_accuracy(tmp_path, monkeypatch, capsys):
    at = datetime(2026, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[LedgerSlot(
        slot_start=at, components={"solar_generation": ComponentFlow(kwh=1, provenance="boundary")},
    )]))
    monkeypatch.setattr(EveningForecastStore, "slots", AsyncMock(return_value=[
        SolarForecastSlot(start=at, p50_kwh=2),
    ]))
    settings = Settings(db_path=str(tmp_path / "household.db"), timezone="UTC")
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    assert cli.main(["stats", "--range", "day", "--at", "2026-01-02"]) == 0
    out = capsys.readouterr().out
    assert "2026-01-02T00:00+00:00 P10=— P50=2 kWh P90=— measured=1 kWh error=1 kWh" in out
    assert "Solar accuracy: MAE=1 kWh; bias (forecast − measured)=1 kWh" in out
    assert "Solar coverage: 1/48 slots (2.1%)" in out


def test_stats_cli_explains_missing_evening_plan(tmp_path, monkeypatch, capsys):
    settings = Settings(db_path=str(tmp_path / "household.db"), timezone="UTC")
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    assert cli.main(["stats", "--range", "day", "--at", "2026-01-02"]) == 0
    assert "Solar coverage: 0/48 slots (0.0%); no evening plan" in capsys.readouterr().out


def test_stats_cli_degrades_failed_forecast_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        EveningForecastStore, "slots", AsyncMock(side_effect=OSError("private-path")),
    )
    settings = Settings(db_path=str(tmp_path / "household.db"), timezone="UTC")
    monkeypatch.setattr(cli, "load_settings", lambda **_: settings)
    assert cli.main(["stats", "--range", "day", "--at", "2026-01-02"]) == 0
    out = capsys.readouterr().out
    assert "Solar coverage: unavailable" in out
    assert "private-path" not in out
