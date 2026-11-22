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


async def _stats_text(tmp_path, monkeypatch, capsys, rows, *, event=True):
    settings = settings_for(tmp_path).model_copy(update={"timezone": "UTC"})
    if event:
        recorder = AxleEventRecorder()
        revision = plan_revision()
        recorder.capture_native(snapshot("accepted"), settings, revision)
        await recorder.record_tick(settings, ACCEPTED, revision, True)
    monkeypatch.setattr(EnergyLedger, "slots", AsyncMock(return_value=rows))
    assert await _cmd_stats(settings, kind="day", at=START.date().isoformat()) == 0
    return capsys.readouterr().out


async def test_a_priced_event_that_earned_nothing_shows_zero(tmp_path, monkeypatch, capsys):
    text = await _stats_text(tmp_path, monkeypatch, capsys, [
        row(exported=0), row(START + HALF_HOUR, exported=0),
    ])
    assert f"Axle earnings (estimated) {START.date()}: 0.00 GBP" in text


async def test_a_day_without_events_hides_the_earnings_line(tmp_path, monkeypatch, capsys):
    text = await _stats_text(tmp_path, monkeypatch, capsys, [
        row(exported=0), row(START + HALF_HOUR, exported=0),
    ], event=False)
    assert f"Axle earnings (estimated) {START.date()}" not in text


async def test_an_event_without_an_estimate_stays_unknown(tmp_path, monkeypatch, capsys):
    text = await _stats_text(tmp_path, monkeypatch, capsys, [row(exported=0)])
    assert f"Axle earnings (estimated) {START.date()}: unknown" in text
