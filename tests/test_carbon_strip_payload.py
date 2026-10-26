"""The carbon strip payload carries exactly the rows the UI needs."""

from datetime import UTC, datetime, timedelta

import pytest

from ha_spark.api import server
from ha_spark.api.server import AppState
from ha_spark.energy.carbon import _store
from tests.api_helpers import owner_client
from tests.test_owner_mode import settings_at

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


@pytest.mark.anyio
async def test_carbon_strip_payload(tmp_path, monkeypatch):
    """Rows cover past, current and future periods, with a gap and attribution."""

    class Clock:
        @staticmethod
        def now(tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(server, "datetime", Clock)
    settings = settings_at(tmp_path).model_copy(update={"carbon_region": 12})
    state = AppState(settings, tmp_path / "options.json")
    region = 12
    rows = [
        (NOW - timedelta(hours=1), NOW - timedelta(minutes=30), 50.0, "very low"),
        (NOW, NOW + timedelta(minutes=30), 120.0, "low"),
        (NOW + timedelta(minutes=30), NOW + timedelta(hours=1), 150.0, "moderate"),
        (NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=30), 250.0, "high"),
        (NOW + timedelta(hours=1, minutes=30), NOW + timedelta(hours=2), 350.0, "very high"),
    ]
    # 11:30-12:00 is intentionally absent, creating the requested gap.
    await _store(settings.db_path, region, NOW,
                 [(start.isoformat(), end.isoformat(), gco2, band)
                  for start, end, gco2, band in rows])
    with owner_client(state) as client:
        response = client.get("/api/carbon")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["region"] == region
    assert payload["fetched_at"] == NOW.isoformat()
    assert payload["ends_at"] == rows[-1][1].isoformat()
    assert payload["attribution"] == (
        "Carbon intensity data: NESO Carbon Intensity API, CC BY 4.0"
    )
    assert payload["rows"] == [
        {"start": rows[0][0].isoformat(), "end": rows[0][1].isoformat(),
         "gco2": 50.0, "index": "very low", "estimated": True},
        {"start": rows[1][0].isoformat(), "end": rows[1][1].isoformat(),
         "gco2": 120.0, "index": "low", "estimated": False},
        {"start": rows[2][0].isoformat(), "end": rows[2][1].isoformat(),
         "gco2": 150.0, "index": "moderate", "estimated": False},
        {"start": rows[3][0].isoformat(), "end": rows[3][1].isoformat(),
         "gco2": 250.0, "index": "high", "estimated": False},
        {"start": rows[4][0].isoformat(), "end": rows[4][1].isoformat(),
         "gco2": 350.0, "index": "very high", "estimated": False},
    ]
