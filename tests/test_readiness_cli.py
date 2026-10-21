"""The readiness slot diagnostic is read-only, including an empty install."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ha_spark.cli import main
from ha_spark.config import Settings
from ha_spark.energy.readiness_ledger import record_slot
from tests.test_readiness_ledger import START, revision


def test_readiness_slots_lists_records_and_gaps(tmp_path, monkeypatch, capsys):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return START.replace(hour=1)

    monkeypatch.setattr("ha_spark.cli.datetime", Clock)
    a = revision()
    # Keep a real historical row while asking for enough days to include it.
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="UTC",
                        db_path=str(tmp_path / "ledger.db"))
    asyncio.run(record_slot(settings.db_path, START, a, "observe", a.run.inputs.soc))
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "slots", "--days", "1"]) == 0
    text = capsys.readouterr().out
    assert "2026-10-20: 1/3 slots recorded" in text
    assert "2026-10-20T00:00:00+00:00 observe A" in text
    assert "gap 2026-10-20T00:30:00+00:00" in text
    assert "gap 2026-10-20T01:00:00+00:00" in text


def test_readiness_slots_empty_database_is_read_only(tmp_path, monkeypatch, capsys):
    settings = Settings(db_path=str(tmp_path / "missing.db"))
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "slots", "--days", "1"]) == 0
    assert "gap" in capsys.readouterr().out
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("month,day,slots", [(3, 29, 46), (10, 25, 50)])
def test_readiness_slots_counts_household_local_dst_day(
    tmp_path, monkeypatch, capsys, month, day, slots,
):
    zone = ZoneInfo("Europe/London")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, month, day, 23, 30, tzinfo=zone)

    settings = Settings(timezone="Europe/London", db_path=str(tmp_path / "missing.db"))
    monkeypatch.setattr("ha_spark.cli.datetime", Clock)
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "slots", "--days", "1"]) == 0
    assert f"0/{slots} slots recorded" in capsys.readouterr().out


def test_readiness_slots_kathmandu_current_slot_uses_local_grid(tmp_path, monkeypatch, capsys):
    zone = ZoneInfo("Asia/Kathmandu")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 20, 0, 17, tzinfo=zone)

    settings = Settings(timezone="Asia/Kathmandu", db_path=str(tmp_path / "missing.db"))
    monkeypatch.setattr("ha_spark.cli.datetime", Clock)
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "slots", "--days", "1"]) == 0
    text = capsys.readouterr().out
    assert "2026-10-20: 0/1 slots recorded" in text
    assert "gap 2026-10-19T18:15:00+00:00" in text
    assert "18:45:00" not in text
    assert "19:15:00" not in text


def test_readiness_slots_requires_positive_days(tmp_path, monkeypatch):
    settings = Settings(db_path=str(tmp_path / "missing.db"))
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "slots", "--days", "0"]) == 2
