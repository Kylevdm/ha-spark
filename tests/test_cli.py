"""Light CLI tests for the consumption import commands."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark import cli
from ha_spark.cli import (
    _cmd_ask,
    _cmd_backfill_load,
    _cmd_backtest,
    _cmd_context,
    _cmd_forecast_eval,
    _cmd_import_csv,
    _cmd_learn_factors,
    _cmd_onboard,
    _cmd_run,
    build_parser,
)
from ha_spark.config import Settings
from ha_spark.health import CheckResult, Status
from ha_spark.router import RouterResult

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


def test_help_mentions_every_command_and_flags() -> None:
    parser = build_parser()
    top = parser.format_help()
    for command in (
        "states", "health", "onboard", "plan", "ask", "run",
        "backfill-load", "import-csv", "pull-consumption", "backtest", "forecast-eval",
        "context", "learn-factors",
    ):
        assert command in top
    assert "examples:" in top

    args = parser.parse_args(["backtest", "--days", "7"])
    assert args.command == "backtest" and args.days == 7
    args = parser.parse_args(["forecast-eval", "--days", "5"])
    assert args.command == "forecast-eval" and args.days == 5
    args = parser.parse_args(
        ["context", "add", "away", "--from", "2026-07-01", "--to", "2026-07-14"]
    )
    assert args.command == "context" and args.context_command == "add"
    assert args.kind == "away" and args.start == "2026-07-01" and args.end == "2026-07-14"
    args = parser.parse_args(["context", "remove", "3"])
    assert args.context_command == "remove" and args.id == 3
    args = parser.parse_args(["plan", "--apply"])
    assert args.apply is True
    args = parser.parse_args(["ask", "what's", "the", "plan"])
    assert args.command == "ask" and args.message == ["what's", "the", "plan"]


async def test_backtest_rates_seeded_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime, timedelta

    from ha_spark.energy.models import ConsumptionInterval
    from ha_spark.energy.store import ConsumptionStore

    settings = Settings(db_path=str(tmp_path / "test.db"), timezone="UTC")
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    async with ConsumptionStore(settings.db_path) as store:
        await store.upsert(
            [ConsumptionInterval(start, start + timedelta(minutes=30), 2.0)], "test"
        )

    assert await _cmd_backtest(settings, days=7) == 0
    out = capsys.readouterr().out
    assert "Grid import backtest" in out
    assert "2.00 kWh" in out


async def test_backtest_empty_store_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "empty.db"))
    assert await _cmd_backtest(settings, days=7) == 2
    assert "No stored consumption" in capsys.readouterr().err


async def test_forecast_eval_no_recorded_forecasts_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "empty.db"))
    assert await _cmd_forecast_eval(settings, days=14) == 2
    assert "No recorded forecasts" in capsys.readouterr().err


async def test_forecast_eval_reports_accuracy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, date, datetime

    from ha_spark.energy.ledger import ForecastLedger

    settings = Settings(ha_url="http://ha.test", ha_token="t", db_path=str(tmp_path / "test.db"))
    async with ForecastLedger(settings.db_path) as ledger:
        await ledger.record_forecast(
            datetime(2026, 6, 1, tzinfo=UTC), date(2026, 6, 2), "median", 20.0, None, "median of 7d"
        )

    async def fake_stats(*args: object, **kwargs: object) -> list[dict[str, object]]:
        return [{"start": 1780358400000, "change": 22.0}]  # 2026-06-02 00:00 UTC

    monkeypatch.setattr(cli, "statistics_during_period", fake_stats)
    assert await _cmd_forecast_eval(settings, days=14) == 0
    out = capsys.readouterr().out
    assert "median" in out
    assert "MAE" in out


def _ctx_args(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**kw)


async def test_learn_factors_reports_learned_away_and_occupancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime, timedelta

    from ha_spark.energy.context import ContextStore
    from ha_spark.energy.ledger import ForecastLedger

    settings = Settings(db_path=str(tmp_path / "test.db"), timezone="UTC")
    today = datetime.now(UTC).date()
    # Daily actuals: normal 20 kWh, three away days at 8 kWh -> learned ~0.4.
    rows = []
    away = [today - timedelta(days=d) for d in (10, 9, 8)]
    for d in range(20, 0, -1):
        day = today - timedelta(days=d)
        start_ms = datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000
        rows.append({"start": start_ms, "change": 8.0 if day in away else 20.0})

    async def fake_stats(*args: object, **kwargs: object) -> list[dict[str, object]]:
        return rows

    monkeypatch.setattr(cli, "statistics_during_period", fake_stats)
    async with ContextStore(settings.db_path) as store:
        await store.add("away", away[0], away[-1])
    async with ForecastLedger(settings.db_path) as ledger:
        for d in range(14, 0, -1):
            day = today - timedelta(days=d)
            ts = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
            await ledger.record_signal(ts, "occupancy_home_frac", 0.5)

    assert await _cmd_learn_factors(settings) == 0
    out = capsys.readouterr().out
    assert "Away load factor" in out
    assert "0.4" in out
    assert "Occupancy tomorrow" in out


async def test_learn_factors_reports_insufficient_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"), timezone="UTC")

    async def fake_stats(*args: object, **kwargs: object) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(cli, "statistics_during_period", fake_stats)
    assert await _cmd_learn_factors(settings) == 0
    out = capsys.readouterr().out
    assert "not enough away history" in out


async def test_context_add_list_remove_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"), timezone="UTC")

    add = _ctx_args(
        context_command="add", kind="away", start="2026-07-01", end="2026-07-14",
        note="Italy", factor=None,
    )
    assert await _cmd_context(settings, add) == 0
    assert "Added context" in capsys.readouterr().out

    lst = _ctx_args(context_command="list")
    assert await _cmd_context(settings, lst) == 0
    out = capsys.readouterr().out
    assert "away" in out and "Italy" in out and "×0.40" in out

    rm = _ctx_args(context_command="remove", id=1)
    assert await _cmd_context(settings, rm) == 0
    assert "Removed context [1]" in capsys.readouterr().out


async def test_context_add_bad_date_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    add = _ctx_args(
        context_command="add", kind="away", start="not-a-date", end=None,
        note=None, factor=None,
    )
    assert await _cmd_context(settings, add) == 2
    assert "Bad date" in capsys.readouterr().err


async def test_context_remove_missing_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    rm = _ctx_args(context_command="remove", id=99)
    assert await _cmd_context(settings, rm) == 2
    assert "No context with id 99" in capsys.readouterr().err


async def test_context_list_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(db_path=str(tmp_path / "test.db"))
    lst = _ctx_args(context_command="list")
    assert await _cmd_context(settings, lst) == 0
    assert "No context facts stored" in capsys.readouterr().out


def _states_json() -> list[dict[str, object]]:
    def s(eid: str, **attrs: object) -> dict[str, object]:
        return {"entity_id": eid, "state": "1", "attributes": attrs}

    return [
        s("sensor.solisac_battery_soc", device_class="battery", unit_of_measurement="%"),
        s("number.solisac_timed_charge_current", unit_of_measurement="A"),
        s("sensor.historic_household_usage", device_class="energy", unit_of_measurement="kWh"),
    ]


@respx.mock
async def test_onboard_discovery_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def ok(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.OK, "ready")

    monkeypatch.setattr(cli, "check_load_history", ok)
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=_states_json())
    )
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    rc = await _cmd_onboard(settings, as_json=False, write=False, preset_name=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Entity discovery" in out
    assert "sensor.solisac_battery_soc" in out
    assert "soc_entity" in out


@respx.mock
async def test_onboard_json_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(200, json=_states_json())
    )
    settings = Settings(
        ha_url="http://ha.test", ha_token="t", soc_entity="sensor.solisac_battery_soc"
    )
    rc = await _cmd_onboard(settings, as_json=True, write=False, preset_name=None)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["soc_entity"]["candidates"][0]["entity_id"] == "sensor.solisac_battery_soc"
    assert payload["soc_entity"]["status"] == "match"


@respx.mock
async def test_onboard_write_fragment_uses_preset_for_gaps(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def ok(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.OK, "ready")

    monkeypatch.setattr(cli, "check_load_history", ok)
    # Only the SoC entity is discoverable; the dispatch sensor must come from the preset.
    respx.get("http://ha.test/api/states").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "entity_id": "sensor.solisac_battery_soc",
                    "state": "1",
                    "attributes": {"device_class": "battery", "unit_of_measurement": "%"},
                }
            ],
        )
    )
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    rc = await _cmd_onboard(settings, as_json=False, write=True, preset_name="solis")
    out = capsys.readouterr().out
    assert rc == 0
    assert "soc_entity: sensor.solisac_battery_soc    # discovered" in out
    assert "dispatch_entity:" in out and "# preset" in out


@respx.mock
async def test_onboard_unknown_preset_errors(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    rc = await _cmd_onboard(settings, as_json=False, write=False, preset_name="nope")
    assert rc == 2
    assert "Unknown preset" in capsys.readouterr().err


@respx.mock
async def test_onboard_exit_code_tracks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.OK, "ready")

    async def warn(_s: Settings) -> CheckResult:
        return CheckResult("Load history", Status.WARN, "thin")

    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    monkeypatch.setattr(cli, "check_load_history", ok)
    assert await _cmd_onboard(settings, as_json=False, write=False, preset_name=None) == 0
    monkeypatch.setattr(cli, "check_load_history", warn)
    assert await _cmd_onboard(settings, as_json=False, write=False, preset_name=None) == 2


async def test_ask_prints_routed_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_route(message: str, _s: Settings, _rest: object) -> RouterResult:
        assert message == "what's the plan"
        return RouterResult(text="all good", source="offline")

    monkeypatch.setattr(cli, "route_message", fake_route)
    settings = Settings(ha_url="http://ha.test", ha_token="t")
    assert await _cmd_ask(settings, "what's the plan") == 0
    assert capsys.readouterr().out.strip() == "[offline] all good"


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
