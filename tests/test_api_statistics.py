"""The Statistics screen's read-only calendar requests through ingress."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.api import history, server
from ha_spark.energy.axle_events import AxleEvent
from ha_spark.energy.energy_ledger import ComponentFlow, EnergyLedger, LedgerSlot
from ha_spark.energy.evening_forecast import EveningForecastStore, SolarForecastSlot
from ha_spark.energy.models import ConsumptionInterval
from tests.api_helpers import owner_client
from tests.test_api_history import _frozen_server_clock, _state
from tests.test_stats_cost import row
from tests.test_stats_load import forecast
from tests.test_stats_ranges import LONDON, priced_row


@pytest.mark.parametrize(("kind", "anchor", "start", "end", "resolution", "slots"), [
    ("day", "2026-03-29", "2026-03-29T00:00", "2026-03-29T23:00", "hour", 46),
    ("day", "2026-10-25", "2026-10-24T23:00", "2026-10-26T00:00", "hour", 50),
    ("week", "2026-10-25", "2026-10-18T23:00", "2026-10-26T00:00", "day", 338),
    ("month", "2026-10-25", "2026-09-30T23:00", "2026-11-01T00:00", "day", 1490),
    ("year", "2026-10-25", "2026-01-01T00:00", "2027-01-01T00:00", "day", 17520),
    ("all", None, "2026-07-31T23:00", "2027-01-02T12:00", "month", 7418),
])
def test_statistics_calendar(tmp_path, monkeypatch, kind, anchor, start, end, resolution, slots):
    now = datetime(2027, 1, 2, 12, tzinfo=UTC)
    _frozen_server_clock(monkeypatch, now)
    earliest = datetime(2026, 8, 3, tzinfo=LONDON)
    monkeypatch.setattr(EnergyLedger, "first_slot", AsyncMock(return_value=earliest))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        params = {"range": kind, **({"at": anchor} if anchor else {})}
        response = client.get("/api/history", params=params)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["resolution"] == resolution
    assert body["range"]["kind"] == kind
    assert body["range"]["at"] == (anchor or "2027-01-02")
    assert body["range"]["today"] == "2027-01-02"
    assert datetime.fromisoformat(body["range"]["from"]) == datetime.fromisoformat(start + "+00:00")
    assert datetime.fromisoformat(body["range"]["to"]) == datetime.fromisoformat(end + "+00:00")
    assert body["range_totals"]["total_slots"] == slots


@pytest.mark.parametrize("params", [
    {"range": "private"}, {"range": "day", "at": "private"},
    {"range": "day", "at": "20261025"}, {"range": "year", "at": "9999-01-01"},
    {"range": "day", "at": "0001-01-01"}, {"range": "day", "at": "2026-02-30"},
    {"range": "day", "resolution": "day"}, {"range": "week", "resolution": "hour"},
    {"range": "day", "from": "private"},
    {"range": "day", "to": "private"}, [("range", "private"), ("range", "day")],
    [("range", "day"), ("at", "private"), ("at", "2026-10-01")],
    {"range": "all", "at": "0001-01-01"},
])
def test_statistics_invalid_calendar(tmp_path, monkeypatch, params):
    _frozen_server_clock(monkeypatch, datetime(2026, 10, 25, 12, tzinfo=UTC))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        response = client.get("/api/history", params=params)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid history range"}


def test_statistics_current_day_preserves_gaps_and_unpriced_measurements(tmp_path, monkeypatch):
    start = datetime(2026, 10, 25, tzinfo=LONDON).astimezone(UTC)
    _frozen_server_clock(monkeypatch, start + timedelta(minutes=90))
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[
        priced_row(start, 2, "distributed"),
        priced_row(start + timedelta(hours=1), 1, "hourly_backfill"),
    ]))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        response = client.get("/api/history", params={"range": "day", "resolution": "slot"})
    assert response.status_code == 200
    body = response.json()
    assert body["range_totals"]["total_slots"] == 3
    assert body["range_totals"]["priced_slots"] == 0
    assert body["range_totals"]["lower_resolution"] is True
    assert body["provenance_counts"] == {"distributed": 1, "gap": 1, "hourly_backfill": 1}
    first, gap, last = body["buckets"]
    assert first["flows"]["grid_import"] == {"kwh": 2, "provenance": "distributed"}
    assert gap["components_kwh"]["grid_import"] is None
    assert gap["cost_reason"] == "missing_slot"
    assert all(slot["actual_cost"] is None and slot["cost_reason"] for slot in (first, gap, last))


def test_statistics_today_is_on_the_household_clock(tmp_path, monkeypatch):
    now = datetime(2026, 6, 1, 23, 31, tzinfo=UTC)
    _frozen_server_clock(monkeypatch, now)
    state = _state(tmp_path, timezone=LONDON.key)
    with owner_client(state) as client:
        response = client.get("/api/history?range=day")
    assert response.status_code == 200
    body = response.json()
    assert body["range"]["today"] == "2026-06-02"
    assert body["range"]["to"] == "2026-06-02T00:31:00+01:00"
    assert body["range_totals"]["total_slots"] == 2
    assert not (tmp_path / "db.sqlite").exists()


@pytest.mark.parametrize("kind", ["week", "month", "year"])
def test_statistics_matches_cli_with_axle(tmp_path, monkeypatch, capsys, kind):
    _frozen_server_clock(monkeypatch, datetime(2027, 1, 2, tzinfo=UTC))
    # The CLI counts a period only up to now (#620): give it the server's clock.
    monkeypatch.setattr(cli, "datetime", server.datetime)
    state = _state(tmp_path, timezone=LONDON.key)
    monkeypatch.setattr(cli, "load_settings", lambda **_: state.settings)
    at = datetime(2026, 9, 1, 8, tzinfo=LONDON)
    event = AxleEvent(start=at, end=at + timedelta(minutes=30),
                      accepted_at=at - timedelta(hours=1), committed_kwh=1, rate_gbp_kwh=1.5)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[row(at)]))
    for module in (cli, server):
        monkeypatch.setattr(module, "window_evidence", AsyncMock(return_value=([event], [])))
    assert cli.main(["stats", "--range", kind, "--at", "2026-09-01"]) == 0
    printed = capsys.readouterr().out
    with owner_client(state) as client:
        response = client.get("/api/history", params={"range": kind, "at": "2026-09-01"})
    assert response.status_code == 200
    body = response.json()
    totals = body["range_totals"]
    for field, label in [("actual_cost", "Actual cost"), ("no_battery_cost", "No-battery cost"),
                         ("battery_savings", "Battery savings")]:
        assert f"{label}={totals[field]:.2f} GBP" in printed
    assert f'Coverage: 1/{totals["total_slots"]} slots' in printed
    assert f'Cost coverage: 1/{totals["total_slots"]} slots' in printed
    axle = body["axle"]
    assert axle["delivery_rate"] == 1
    assert {"date": "2026-09-01", "earnings_gbp": 1.5} in axle["earnings_by_day"]
    assert axle["events"] == [{"start": at.isoformat(), "end": event.end.isoformat(),
                               "outcome": "delivered", "delivered_kwh": 1, "earnings_gbp": 1.5}]
    assert f'Axle delivery rate: {axle["delivery_rate"]:.1%}' in printed
    assert "Axle earnings (estimated) 2026-09-01: 1.50 GBP" in printed


@pytest.mark.parametrize("kind", ["day", "week"])
def test_statistics_exposes_accuracy_reconciliation_and_session(tmp_path, monkeypatch, kind):
    start = datetime(2026, 9, 1, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, start + timedelta(days=7))
    half_hour = timedelta(minutes=30)
    instants = [start + index * half_hour for index in range(48)]
    rows = [priced_row(at, 2, "statistics_fill") for at in instants]
    rows[0] = rows[0].model_copy(update={"import_session": "free_electricity"})
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=rows))
    monkeypatch.setattr(EveningForecastStore, "slots", AsyncMock(return_value=[
        SolarForecastSlot(start=start, p10_kwh=0, p50_kwh=1, p90_kwh=2),
    ]))
    monkeypatch.setattr(history, "read_forecast_history", AsyncMock(return_value=[
        forecast(start.date(), total=47, median_total=50, slots=[1] * 48, median_slots=[2] * 48),
    ]))
    monkeypatch.setattr(history, "read_consumption", AsyncMock(return_value=[
        ConsumptionInterval(at, at + half_hour, 1) for at in instants
    ]))
    state = _state(tmp_path, timezone=LONDON.key)
    with owner_client(state) as client:
        params = {"range": kind, "at": "2026-09-01"}
        if kind == "day":  # The Day view's half-hourly toggle (#753).
            params["resolution"] = "slot"
        response = client.get("/api/history", params=params)
    assert response.status_code == 200
    body = response.json()
    solar = body["solar_accuracy"]
    assert solar["mae_kwh"] == solar["bias_kwh"] == 1
    assert solar["scored_slots"] == 1
    assert solar["coverage"] == 1 / solar["total"]
    assert solar["reason"] is None
    if kind == "day":
        assert solar["slots"][0] == {
            "start": start.isoformat(), "p10_kwh": 0, "p50_kwh": 1, "p90_kwh": 2,
            "measured_kwh": 0, "error_kwh": 1, "band_hit": True,
        }
        slot = body["buckets"][0]
        assert slot["flows"]["grid_import"]["provenance"] == "statistics_fill"
        assert slot["cost_inputs"]["import_session"] == "free_electricity"
        assert slot["cost_inputs"]["free_import_kwh"] == 2
        assert slot["actual_cost"] == 0
        assert slot["cost_reason"] is None
    else:
        assert "slots" not in solar
    assert {m["model"]: (m["daily_mae_kwh"], m["shape_mae_kwh"])
            for m in body["load_accuracy"]["models"]} == {"ml": (1, 0), "median": (2, 1)}
    assert body["load_accuracy"]["days"][0]["day"] == start.date().isoformat()
    meter = body["meter_reconciliation"]
    assert meter["by_day"]["2026-09-01"] == meter["range_fraction"] == 1
    assert meter["covered_days"] == 1


@pytest.mark.parametrize(("owner", "reader", "key", "reason", "warning"), [
    (EveningForecastStore, "slots", "solar_accuracy", "evening_forecast_unreadable",
     "Evening-plan forecast read failed"),
    (history, "read_forecast_history", "load_accuracy", "forecast_history_unreadable",
     "Load forecast history read failed"),
    (history, "read_consumption", "meter_reconciliation", "meter_history_unreadable",
     "Meter history read failed"),
    (EnergyLedger, "slots", "statistics", "ledger_unreadable", "Energy ledger read failed"),
    (server, "window_evidence", "statistics", "ledger_unreadable", "Energy ledger read failed"),
    (EnergyLedger, "first_slot", "statistics", "ledger_unreadable", "Energy ledger read failed"),
])
def test_statistics_degrades_one_source(tmp_path, monkeypatch, caplog, owner, reader, key,
                                        reason, warning):
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 1, 12, tzinfo=LONDON))
    monkeypatch.setattr(owner, reader, AsyncMock(side_effect=OSError("private-payload")))
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history?range=all")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["unavailable"][key] == reason
    blocks = {"solar_accuracy", "load_accuracy", "meter_reconciliation", "axle"}
    missing = "axle" if key == "statistics" else key
    assert body.keys() & blocks == blocks - {missing}
    assert "private-payload" not in response.text
    assert [record.getMessage() for record in caplog.records
            if record.name in {history.__name__, server.__name__}] == [warning]


@pytest.mark.parametrize(("status", "peer", "token", "repair"), [
    (404, "testclient", True, False), (403, "ingress-peer", False, False),
    (503, "testclient", False, True),
])
def test_statistics_keeps_ingress_and_repair_gates(tmp_path, status, peer, token, repair):
    state = _state(tmp_path)
    if repair:
        state.settings_service.repair = ["invalid configuration"]
    app = server.build_app(state, ingress_peer=peer, require_token=token, token="TOKEN")
    with TestClient(app) as client:
        response = client.get("/api/history?range=day", headers={
            "Authorization": "Bearer TOKEN", "X-Forwarded-For": peer, "X-Remote-User-Id": "owner",
        })
    assert response.status_code == status


def test_statistics_daily_buckets_mark_partial_days_and_count_rejected(tmp_path, monkeypatch):
    start = datetime(2026, 8, 30, 23, tzinfo=UTC)  # household Monday 31 August
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 8, 12, tzinfo=UTC))
    half_hour = timedelta(minutes=30)
    rows = [priced_row(start + i * half_hour, 2) for i in range(48)]
    rows.append(LedgerSlot(slot_start=start + 48 * half_hour, components={
        "grid_import": ComponentFlow(reason="rejected"),
    }))
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=rows))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        body = client.get("/api/history", params={"range": "week", "at": "2026-09-01"}).json()
    assert body["resolution"] == "day"
    assert [bucket["partial"] for bucket in body["buckets"]] == (
        [False, True, True, True, True, True, True])
    assert body["provenance_counts"]["rejected"] == 1
    first, second = body["buckets"][:2]
    assert first["actual_cost"] == 24  # 48 measured half-hours at the captured rates
    assert second["actual_cost"] is None


def test_statistics_year_shows_one_bar_per_day(tmp_path, monkeypatch):
    _frozen_server_clock(monkeypatch, datetime(2027, 1, 2, 12, tzinfo=UTC))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        body = client.get("/api/history", params={"range": "year", "at": "2026-06-01"}).json()
    assert body["resolution"] == "day"
    starts = [bucket["start"] for bucket in body["buckets"]]
    assert len(starts) == 365  # 2026 is not a leap year
    assert len(set(starts)) == 365
    assert starts[0].startswith("2026-01-01") and starts[-1].startswith("2026-12-31")


def test_statistics_calendar_day_carries_no_forecast_total(tmp_path, monkeypatch):
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 2, tzinfo=UTC))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        body = client.get("/api/history", params={"range": "day", "at": "2026-09-01"}).json()
    assert body["totals"] == {"solar_forecast_kwh": None, "solar_measured_kwh": None}
    assert body["unavailable"]["solar_forecast"] == "slot_resolution_only"
    # Hourly ledger buckets never carry the plan view's solar series.
    assert all("solar_forecast_kwh" not in bucket for bucket in body["buckets"])


def test_statistics_failed_ledger_reports_no_provenance(tmp_path, monkeypatch):
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 2, tzinfo=UTC))
    monkeypatch.setattr(server, "window_evidence", AsyncMock(side_effect=OSError("x")))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        body = client.get("/api/history", params={"range": "day", "at": "2026-09-01"}).json()
    assert body["unavailable"]["statistics"] == "ledger_unreadable"
    assert "provenance_counts" not in body


def test_statistics_axle_event_times_are_household(tmp_path, monkeypatch):
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 2, tzinfo=UTC))
    at = datetime(2026, 9, 1, 7, tzinfo=UTC)
    event = AxleEvent(start=at, end=at + timedelta(minutes=30),
                      accepted_at=at - timedelta(hours=1), committed_kwh=1, rate_gbp_kwh=1.5)
    monkeypatch.setattr(server, "window_evidence", AsyncMock(return_value=([event], [])))
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        body = client.get("/api/history", params={"range": "day", "at": "2026-09-01"}).json()
    assert body["axle"]["events"][0]["start"] == "2026-09-01T08:00:00+01:00"


def test_statistics_failed_block_computation_degrades_alone(tmp_path, monkeypatch, caplog):
    _frozen_server_clock(monkeypatch, datetime(2026, 9, 2, tzinfo=UTC))
    monkeypatch.setattr(history, "meter_reconciliation", lambda *_: 1 / 0)
    with owner_client(_state(tmp_path, timezone=LONDON.key)) as client:
        response = client.get("/api/history", params={"range": "day", "at": "2026-09-01"})
    assert response.status_code == 200
    body = response.json()
    assert body["unavailable"]["meter_reconciliation"] == "meter_history_unreadable"
    assert "meter_reconciliation" not in body
    assert {"solar_accuracy", "load_accuracy"} <= body.keys()
    assert [r.getMessage() for r in caplog.records if r.name == history.__name__] == [
        "Meter history read failed"]
