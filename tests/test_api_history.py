"""`GET /api/history`: the today-so-far series for the plan view (#505).

Phase 6 serves the `slot` resolution over a range inside today only, on the
ingress app. Buckets step by absolute half-hours from local midnight in the
household zone, the recorded solar forecast reads per slot, and measured solar
is unavailable until ticket 10 fills it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import INGRESS_PROXY_IP, AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy.solar_forecasts import SolarForecastStore
from tests.api_helpers import owner_client
from tests.authority_helpers import seed_settings

LONDON = ZoneInfo("Europe/London")


def _state(tmp_path, **settings_kw) -> AppState:
    settings_kw.setdefault("db_path", str(tmp_path / "db.sqlite"))
    service = seed_settings(
        tmp_path, settings=Settings(ha_url="http://ha.test", ha_token="t", **settings_kw)
    )
    return AppState(service.settings, tmp_path / "options.json", settings_service=service)


def _frozen_server_clock(monkeypatch: pytest.MonkeyPatch, frozen: datetime) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _Clock:
            at = frozen.astimezone(tz) if tz is not None else frozen.replace(tzinfo=None)
            return cls.fromtimestamp(at.timestamp(), at.tzinfo)

    monkeypatch.setattr("ha_spark.api.server.datetime", _Clock)


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize(
    ("params",),
    [
        ({"to": "2026-01-12T15:10:00+00:00"},),  # no resolution
        ({"resolution": "day", "from": "x", "to": "x"},),
        ({"resolution": "slot"},),  # no from/to
        ({"resolution": "slot", "to": "2026-01-12T15:10:00+00:00"},),  # no from
        ({"resolution": "slot", "from": "2026-01-12T00:00:00"},),  # no to
        # Naive timestamps are rejected: an instant needs its offset.
        ({"resolution": "slot", "from": "2026-01-12T00:00:00",
          "to": "2026-01-12T15:10:00"},),
        ({"resolution": "slot", "from": "not-a-time", "to": "not-a-time"},),
        ({"resolution": "slot", "from": "2026-01-12", "to": "2026-01-12"},),
    ],
)
def test_history_rejects_malformed_queries(tmp_path, params):
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history", params=params)
    assert response.status_code == 400
    assert response.json()["error"] in {"unsupported resolution", "invalid history range"}


@pytest.mark.parametrize(
    ("from_iso", "to_iso"),
    [
        # A range that leaves today: before local midnight, or ending in the future.
        ("2026-01-11T23:00:00+00:00", "2026-01-12T01:10:00+00:00"),
        ("2026-01-12T00:00:00+00:00", "2026-01-13T01:10:00+00:00"),
        # An inverted range.
        ("2026-01-12T02:10:00+00:00", "2026-01-12T01:10:00+00:00"),
    ],
)
def test_history_rejects_ranges_outside_today(tmp_path, monkeypatch, from_iso, to_iso):
    _frozen_server_clock(monkeypatch, datetime(2026, 1, 12, 1, 10, tzinfo=LONDON))
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history", params={
            "resolution": "slot", "from": from_iso, "to": to_iso,
        })
    assert response.status_code == 400
    assert response.json() == {"error": "history range must be inside today"}


def test_history_serves_a_range_inside_today_with_any_zone_offset(tmp_path):
    now = datetime.now(LONDON)
    midnight = _midnight(now)
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history", params={
            "resolution": "slot",
            # The same midnight instant expressed from another zone is fine.
            "from": midnight.astimezone(ZoneInfo("Asia/Kathmandu")).isoformat(),
            "to": now.isoformat(),
        })
    assert response.status_code == 200


async def test_history_buckets_and_totals(tmp_path, monkeypatch):
    """Buckets start at local midnight whatever `from` says; totals agree with them."""
    frozen = datetime(2026, 1, 12, 15, 10, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    midnight = _midnight(frozen).astimezone(UTC)
    state = _state(tmp_path)
    slot_a, slot_b = midnight + timedelta(minutes=30), midnight + timedelta(hours=1)
    async with SolarForecastStore(state.settings.db_path) as store:
        # The plan issued before the slots began is the one recorded; an issue
        # during or at a slot's own start never counts for that slot.
        await store.record(midnight - timedelta(hours=1), [(slot_a, 1.0)])
        await store.record(slot_a + timedelta(minutes=5), [(slot_a, 5.0)])
        await store.record(slot_b, [(slot_b, 7.0)])
    with owner_client(state) as client:
        response = client.get("/api/history", params={
            "resolution": "slot", "from": (midnight + timedelta(hours=2)).isoformat(),
            "to": frozen.isoformat(),
        })
    assert response.status_code == 200
    body = response.json()
    starts = [datetime.fromisoformat(bucket["start"]) for bucket in body["buckets"]]
    expected = []
    at = midnight
    while at < frozen:
        expected.append(at)
        at += timedelta(minutes=30)
    assert starts == expected  # 31 buckets, from midnight, not from `from`
    values = [bucket["solar_forecast_kwh"] for bucket in body["buckets"]]
    assert values == [None, 1.0, None, *[None] * (len(values) - 3)]
    assert all(bucket["solar_measured_kwh"] is None for bucket in body["buckets"])
    assert body["totals"] == {"solar_forecast_kwh": 1.0, "solar_measured_kwh": None}
    assert body["coverage"] is None
    assert body["unavailable"] == {"solar_measured": "no_entity"}


def test_history_unavailable_reasons(tmp_path):
    entity = {"derive_solar_generation_entity": "sensor.solar_gen"}
    for extra, reason in (({}, "no_entity"), (entity, "not_implemented")):
        now = datetime.now(LONDON)
        with owner_client(_state(tmp_path, **extra)) as client:
            body = client.get("/api/history", params={
                "resolution": "slot", "from": _midnight(now).isoformat(), "to": now.isoformat(),
            }).json()
        assert body["unavailable"] == {"solar_measured": reason}
        assert body["totals"]["solar_measured_kwh"] is None


@pytest.mark.parametrize(
    ("frozen", "from_iso"),
    [
        (datetime(2026, 10, 25, 2, 10, tzinfo=LONDON), "2026-10-25T00:00:00+01:00"),
        (datetime(2026, 3, 29, 2, 10, tzinfo=LONDON), "2026-03-29T00:00:00+00:00"),
    ],
    ids=["fall-back", "spring-forward"],
)
def test_history_buckets_keep_dst_days_real_slots(tmp_path, monkeypatch, frozen, from_iso):
    _frozen_server_clock(monkeypatch, frozen)
    with owner_client(_state(tmp_path)) as client:
        body = client.get("/api/history", params={
            "resolution": "slot", "from": from_iso, "to": frozen.isoformat(),
        }).json()
    starts = [bucket["start"] for bucket in body["buckets"]]
    if frozen.month == 10:  # the repeated hour appears with both offsets
        assert starts == [
            "2026-10-25T00:00:00+01:00", "2026-10-25T00:30:00+01:00",
            "2026-10-25T01:00:00+01:00", "2026-10-25T01:30:00+01:00",
            "2026-10-25T01:00:00+00:00", "2026-10-25T01:30:00+00:00",
            "2026-10-25T02:00:00+00:00",
        ]
    else:  # the skipped hour never appears
        assert starts == [
            "2026-03-29T00:00:00+00:00", "2026-03-29T00:30:00+00:00",
            "2026-03-29T02:00:00+01:00",
        ]


@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_history_absent_outside_ingress(tmp_path, token, peer):
    state = _state(tmp_path)
    kwargs = {"require_token": token, "token": "TOKEN", "ingress_peer": peer}
    with TestClient(build_app(state, **kwargs)) as client:
        response = client.get("/api/history", params={
            "resolution": "slot", "from": "2026-01-12T00:00:00+00:00",
            "to": "2026-01-12T01:10:00+00:00",
        }, headers={"Authorization": "Bearer TOKEN", "X-Remote-User-Id": "owner"})
    assert response.status_code == 404


def test_history_refuses_a_non_ingress_peer(tmp_path):
    state = _state(tmp_path)
    with TestClient(build_app(state, ingress_peer=INGRESS_PROXY_IP),
                    client=("192.0.2.1", 50000)) as client:
        response = client.get("/api/history", params={
            "resolution": "slot", "from": "2026-01-12T00:00:00+00:00",
            "to": "2026-01-12T01:10:00+00:00",
        }, headers={"X-Forwarded-For": INGRESS_PROXY_IP, "X-Remote-User-Id": "owner"})
    assert response.status_code == 403
