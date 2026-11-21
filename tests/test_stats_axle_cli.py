"""The stats CLI renders Axle figures returned by the statistics function."""

from unittest.mock import AsyncMock

from ha_spark.cli import _cmd_stats
from ha_spark.energy.axle_events import AxleEventRecorder
from ha_spark.energy.energy_ledger import EnergyLedger
from ha_spark.energy.readiness_measurements import HALF_HOUR
from tests.test_axle_events import ACCEPTED, plan_revision, snapshot
from tests.test_energy_ledger import settings_for
from tests.test_stats_cost import START, row


async def test_stats_shows_daily_axle_earnings_and_delivery_rate(tmp_path, monkeypatch, capsys):
    settings = settings_for(tmp_path).model_copy(update={"timezone": "UTC"})
    recorder = AxleEventRecorder()
    revision = plan_revision()
    recorder.capture_native(snapshot("accepted"), settings, revision)
    await recorder.record_tick(settings, ACCEPTED, revision, True)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=[
        row(exported=2), row(START + HALF_HOUR, exported=1),
    ]))
    assert await _cmd_stats(settings, kind="day", at=START.date().isoformat()) == 0
    text = capsys.readouterr().out
    assert f"Axle earnings (estimated) {START.date()}: 3.00 GBP" in text
    assert "Axle delivery rate: 100.0%" in text
