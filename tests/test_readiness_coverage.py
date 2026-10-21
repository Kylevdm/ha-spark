"""Coverage reads stored day-close evidence without contacting HA or creating a DB."""
import asyncio
from datetime import datetime, timedelta

from ha_spark.cli import main
from ha_spark.config import Settings
from ha_spark.energy.readiness_measurements import ReadinessDays, snapshot_day
from tests.test_readiness_ledger import START
from tests.test_readiness_measurements import measured, seed  # noqa: F401


def test_coverage_lists_qualifying_and_excluded_snapshots(
    measured, monkeypatch, capsys,  # noqa: F811 - shared fixture
):
    settings, _ = measured

    async def prepare():
        await seed(settings)
        day = await snapshot_day(settings, START.date())
        await ReadinessDays(settings.db_path).record(day.model_copy(update={
            "local_date": START.date() + timedelta(days=1),
            "reasons": ("grid_import: substituted measurement gap",),
        }))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return START + timedelta(days=2)

    asyncio.run(prepare())
    monkeypatch.setattr("ha_spark.cli.datetime", Clock)
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: settings)
    assert main(["readiness", "coverage", "--days", "2"]) == 0
    output = capsys.readouterr().out
    assert "2026-10-20: qualifying" in output
    assert "2026-10-21: excluded" in output
    assert "substituted measurement gap" in output
    assert "measured=48" in output
    assert "absent=48" in output


def test_coverage_missing_database_is_read_only(tmp_path, monkeypatch, capsys):
    path = tmp_path / "missing.db"
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **kwargs: Settings(db_path=str(path)))
    assert main(["readiness", "coverage", "--days", "1"]) == 0
    assert "day snapshot missing" in capsys.readouterr().out
    assert not path.exists()
    assert main(["readiness", "coverage", "--days", "0"]) == 2
