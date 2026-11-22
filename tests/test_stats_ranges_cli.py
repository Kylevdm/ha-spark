"""CLI ranges render the same ledger figures as the ingress history endpoint."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ha_spark import cli
from ha_spark.api import server as server_module
from ha_spark.energy.energy_ledger import EnergyLedger, RateEvidence
from tests.api_helpers import owner_client
from tests.test_api_history import _frozen_server_clock, _state
from tests.test_stats_ranges import LONDON, priced_row


@pytest.mark.parametrize(("kind", "resolution", "first", "stop"), [
    ("week", "day", "2026-08-30T23:00:00+00:00", "2026-09-06T23:00:00+00:00"),
    ("month", "day", "2026-08-31T23:00:00+00:00", "2026-09-30T23:00:00+00:00"),
    ("year", "day", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
])
def test_stats_cli_and_history_agree(tmp_path, monkeypatch, capsys, kind, resolution, first, stop):
    _frozen_server_clock(monkeypatch, datetime(2027, 1, 2, tzinfo=UTC))
    # The CLI counts a period only up to now (#620): give it the server's clock.
    monkeypatch.setattr(cli, "datetime", server_module.datetime)
    state = _state(tmp_path, timezone=LONDON.key)
    monkeypatch.setattr(cli, "load_settings", lambda **_: state.settings)
    when = datetime(2026, 9, 1, tzinfo=LONDON)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[priced_row(when, 2)]))
    assert cli.main(["stats", "--range", kind, "--at", "2026-09-01"]) == 0
    out = capsys.readouterr().out
    with owner_client(state) as client:
        response = client.get("/api/history", params={"resolution": resolution,
                                                      "from": first, "to": stop})
    assert response.status_code == 200
    body = response.json()
    totals = body["range_totals"]
    assert "Totals: grid_import=" + f'{totals["components_kwh"]["grid_import"]:g} kWh' in out
    assert f'Actual cost={totals["actual_cost"]:.2f} GBP' in out
    assert f'No-battery cost={totals["no_battery_cost"]:.2f} GBP' in out
    assert f'Battery savings={totals["battery_savings"]:.2f} GBP' in out
    assert f'Coverage: 1/{totals["total_slots"]} slots' in out
    assert f'Cost coverage: 1/{totals["total_slots"]} slots' in out
    assert "T00:30" not in out
    assert totals["actual_cost"] == .5
    for bucket in body["buckets"]:
        assert f'{bucket["start"][:10]} grid_import=' in out


def test_stats_all_includes_earliest_backfill_through_now(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path, timezone=LONDON.key)
    monkeypatch.setattr(cli, "load_settings", lambda **_: state.settings)
    now = datetime(2026, 10, 1, 12, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz):
            return now.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    ledger = EnergyLedger(state.settings.db_path)
    earliest = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)  # September on the household clock.

    async def record():
        await ledger.record_boundary(state.settings, now, RateEvidence())
        await ledger.record_hourly_backfill({
            earliest: priced_row(earliest, 2, "hourly_backfill").components,
        })

    asyncio.run(record())
    assert cli.main(["stats", "--range", "all"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-01 grid_import=" in out
    assert "2026-08-01 grid_import=" not in out
    assert "lower resolution (hourly)" in out
    assert f"/{(now - (earliest - timedelta(minutes=30))) // timedelta(minutes=30)} slots" in out
