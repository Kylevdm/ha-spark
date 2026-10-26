"""`GET /api/history`: the today-so-far series for the plan view (#505).

Phase 6 serves the `slot` resolution over a range inside today only, on the
ingress app. Buckets step by absolute half-hours from local midnight in the
household zone, the recorded solar forecast reads per slot, and measured solar
is validated from five-minute statistics.
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
    ("params", "error"),
    [
        ({"to": "2026-01-12T15:10:00+00:00"}, "unsupported resolution"),  # no resolution
        ({"resolution": "day", "from": "x", "to": "x"}, "unsupported resolution"),
        # Naive from, no to.
        ({"resolution": "slot", "from": "2026-01-12T00:00:00"}, "invalid history range"),
        # Parseable but unrepresentable in UTC: a 400, never a 500.
        ({"resolution": "slot", "from": "0001-01-01T00:00:00+14:00"}, "invalid history range"),
        ({"resolution": "slot", "to": "9999-12-31T23:59:59-14:00"}, "invalid history range"),
        # Naive timestamps are rejected: an instant needs its offset.
        ({"resolution": "slot", "from": "2026-01-12T00:00:00",
          "to": "2026-01-12T15:10:00"}, "invalid history range"),
        ({"resolution": "slot", "from": "not-a-time", "to": "not-a-time"},
         "invalid history range"),
        ({"resolution": "slot", "from": "2026-01-12", "to": "2026-01-12"},
         "invalid history range"),
    ],
)
def test_history_rejects_malformed_queries(tmp_path, params, error):
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history", params=params)
    assert response.status_code == 400
    assert response.json() == {"error": error}


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


def test_history_serves_a_range_inside_today_with_any_zone_offset(tmp_path, monkeypatch):
    now = datetime(2026, 10, 25, 15, 10, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, now)
    midnight = _midnight(now)
    with owner_client(_state(tmp_path)) as client:
        response = client.get("/api/history", params={
            "resolution": "slot",
            # The same midnight instant expressed from another zone is fine.
            "from": midnight.astimezone(ZoneInfo("Asia/Kathmandu")).isoformat(),
            "to": now.isoformat(),
        })
    assert response.status_code == 200
    buckets = response.json()["buckets"]
    assert datetime.fromisoformat(buckets[0]["start"]) == midnight
    assert datetime.fromisoformat(buckets[-1]["start"]) < now


async def test_history_buckets_and_totals(tmp_path, monkeypatch):
    """Buckets follow the midnight grid from `from`'s half-hour; totals agree with them."""
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
            "resolution": "slot", "from": (midnight + timedelta(minutes=40)).isoformat(),
            "to": frozen.isoformat(),
        })
    assert response.status_code == 200
    body = response.json()
    starts = [datetime.fromisoformat(bucket["start"]) for bucket in body["buckets"]]
    expected = []
    at = slot_a  # the half-hour holding `from`, on the midnight grid
    while at < frozen:
        expected.append(at)
        at += timedelta(minutes=30)
    assert starts == expected
    values = [bucket["solar_forecast_kwh"] for bucket in body["buckets"]]
    assert values == [1.0, None, *[None] * (len(values) - 2)]
    assert all(bucket["solar_measured_kwh"] is None for bucket in body["buckets"])
    assert body["totals"] == {"solar_forecast_kwh": 1.0, "solar_measured_kwh": None}
    assert body["coverage"] is None
    assert body["unavailable"] == {"solar_measured": "no_entity"}


def test_history_unavailable_reasons(tmp_path, monkeypatch):
    async def no_metadata(*args, **kwargs):
        return []
    monkeypatch.setattr("ha_spark.energy.solar_measured.list_statistic_ids", no_metadata)
    entity = {"derive_solar_generation_entity": "sensor.solar_gen"}
    for extra, reason in (({}, "no_entity"), (entity, "no_statistics")):
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


def test_history_omitted_bounds_mean_today_so_far(tmp_path, monkeypatch):
    # The browser asks for "today so far" without working out the midnight
    # offset: on the fall-back day that is 00:00+01:00, not 00:00+00:00.
    frozen = datetime(2026, 10, 25, 2, 10, tzinfo=LONDON)
    _frozen_server_clock(monkeypatch, frozen)
    with owner_client(_state(tmp_path)) as client:
        both = client.get("/api/history", params={"resolution": "slot"}).json()
        upto = client.get("/api/history", params={
            "resolution": "slot", "to": "2026-10-25T01:30:00+00:00"}).json()
    assert [b["start"] for b in both["buckets"]][0] == "2026-10-25T00:00:00+01:00"
    assert len(both["buckets"]) == 7
    assert [b["start"] for b in upto["buckets"]][-1] == "2026-10-25T01:00:00+00:00"
    assert len(upto["buckets"]) == 5


def test_history_repeated_midnight_starts_at_the_first_one(tmp_path, monkeypatch):
    havana = ZoneInfo("America/Havana")
    # 00:40 in the second occurrence of the repeated midnight hour.
    frozen = datetime(2026, 11, 1, 5, 40, tzinfo=UTC).astimezone(havana)
    assert frozen.fold == 1
    _frozen_server_clock(monkeypatch, frozen)
    with owner_client(_state(tmp_path, timezone="America/Havana")) as client:
        response = client.get("/api/history", params={
            "resolution": "slot", "from": "2026-11-01T00:00:00-04:00",
            "to": frozen.isoformat(),
        })
    assert response.status_code == 200
    starts = [b["start"] for b in response.json()["buckets"]]
    assert starts[0] == "2026-11-01T00:00:00-04:00"
    assert len(starts) == 4  # both occurrences of the repeated hour


@pytest.mark.parametrize("minutes,coverage", [(10, None), (40, 1.0), (70, .5)])
def test_measured_history_totals_coverage_and_repeat(tmp_path, monkeypatch, minutes, coverage):
    from ha_spark.energy.solar_measured import MeasuredSolar

    start = datetime(2026, 1, 12, tzinfo=UTC)
    frozen = start + timedelta(minutes=minutes)
    _frozen_server_clock(monkeypatch, frozen)
    calls = 0

    async def reader(settings, at):
        nonlocal calls
        calls += 1
        return "Wh", [{"start": (start + timedelta(minutes=i * 5)).timestamp() * 1000,
                       "change": 100} for i in range(6)]

    app = build_app(_state(tmp_path, derive_solar_generation_entity="sensor.solar"),
                    ingress_peer="testclient", measured_solar=MeasuredSolar(reader=reader))
    with TestClient(app) as client:
        body = client.get("/api/history?resolution=slot").json()
        assert client.get("/api/history?resolution=slot").json() == body
    assert calls == 1
    assert body["unavailable"] == {}
    assert body["coverage"] == coverage
    assert body["totals"]["solar_measured_kwh"] == pytest.approx(.6 if minutes >= 30 else 0)
    assert body["buckets"][0]["solar_measured_kwh"] == (
        pytest.approx(.6) if minutes >= 30 else None)


def test_overflowing_measured_total_is_unknown_not_a_500():
    import json as _json
    from datetime import UTC, datetime, timedelta

    from ha_spark.api.history import history_payload

    start = datetime(2026, 10, 14, tzinfo=UTC)
    starts = [start + timedelta(minutes=30 * i) for i in range(4)]
    payload = history_payload(starts, {}, UTC, None, {s: 6e307 for s in starts},
                              now=start + timedelta(hours=3))
    assert payload["totals"]["solar_measured_kwh"] is None
    assert [b["solar_measured_kwh"] for b in payload["buckets"]] == [6e307] * 4
    _json.dumps(payload, allow_nan=False)
