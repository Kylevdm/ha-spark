"""NESO data is validated, cached and kept separate from planning."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import aiosqlite
import httpx
import pytest
import respx

from ha_spark.energy.carbon import CarbonRefresher, _validate, carbon_payload
from tests.test_config_secret_sweep import CANARY

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def row(start=NOW, value=132, index="moderate"):
    return {"from": start.isoformat(), "to": (start + timedelta(minutes=30)).isoformat(),
            "intensity": {"forecast": value, "index": index}}


def envelope(rows, region=12):
    return {"data": {"regionid": region, "data": rows}}


@pytest.mark.parametrize("bad", [
    envelope([row(value="132")]), envelope([row(value=True)]),
    envelope([row()], True), envelope([row()], 12.0),
    envelope([row(value=float("nan"))]), envelope([row(value=2001)]),
    envelope([row(index="unknown")]), envelope([row(value=Decimal("132"))]),
])
def test_validate_rejects_non_strict_or_invalid_values(bad):
    assert _validate(bad, 12, NOW, NOW + timedelta(hours=1)) == []


@pytest.mark.parametrize("value", [0, 132, 132.5, 2000])
def test_validate_preserves_numeric_values(value):
    assert _validate(envelope([row(value=value)]), 12, NOW, NOW + timedelta(hours=1)) == [
        (NOW.isoformat(), (NOW + timedelta(minutes=30)).isoformat(), float(value), "moderate"),
    ]


def test_validate_skips_bad_elements_and_ignores_extra_keys():
    good = row()
    good["extra"] = CANARY
    good["intensity"]["extra"] = CANARY
    payload = envelope([None, "bad", good])
    payload["extra"] = CANARY
    payload["data"]["extra"] = CANARY
    assert _validate(payload, 12, NOW, NOW + timedelta(hours=1)) == [
        (NOW.isoformat(), (NOW + timedelta(minutes=30)).isoformat(), 132.0, "moderate"),
    ]


def test_validate_cap_keeps_first_300_keys_but_later_duplicates_win():
    rows = [row(NOW + timedelta(minutes=30 * i)) for i in reversed(range(301))]
    rows.append(row(NOW + timedelta(minutes=30), value=199))
    result = _validate(envelope(rows), 12, NOW, NOW + timedelta(days=7))
    assert result == [
        (start.isoformat(), (start + timedelta(minutes=30)).isoformat(),
         199.0 if i == 1 else 132.0, "moderate")
        for i in range(1, 301)
        for start in [NOW + timedelta(minutes=30 * i)]
    ]


@respx.mock
async def test_carbon_fetch_range_and_off(tmp_path):
    region = None
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: region)
    refresher.tick(NOW, "Europe/London", None)
    assert not respx.calls
    region = 12
    route = respx.get(
        "https://api.carbonintensity.org.uk/regional/intensity/"
        "2026-10-13T23:00Z/2026-10-16T12:00Z/regionid/12"
    ).mock(return_value=httpx.Response(200, json=envelope([row()])))
    refresher.tick(NOW, "Europe/London", None)
    await refresher.wait()
    assert route.call_count == 1
    assert "authorization" not in route.calls[0].request.headers
    payload = await carbon_payload(path, region, NOW)
    assert payload["status"] == "ok"
    assert payload["rows"] == [{"start": NOW.isoformat(),
                                "end": (NOW + timedelta(minutes=30)).isoformat(),
                                "gco2": 132.0, "index": "moderate", "estimated": False}]
    refresher.tick(NOW + timedelta(minutes=29), "Europe/London", None)
    await refresher.wait()
    assert route.call_count == 1
    await refresher.close()


@respx.mock
async def test_region_does_not_change_planner_inputs_plan_or_readiness(tmp_path, monkeypatch):
    from ha_spark.config import Settings
    from ha_spark.energy import sources
    from ha_spark.energy.models import LoadForecast
    from ha_spark.energy.plan_run import current_plan
    from ha_spark.energy.readiness import evaluate_readiness
    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
    from ha_spark.ha.rest import HomeAssistantRest
    from tests.solar_source_helpers import entities_source
    from tests.test_readiness import fixture

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    async def forecast(settings, **kwargs):
        return LoadForecast(total_kwh=24, slots=(0.5,) * 48, source="test",
                            target_date=kwargs["target"])

    monkeypatch.setattr(sources, "datetime", Clock)
    monkeypatch.setattr(sources, "predict_home_load", forecast)
    respx.route(method="GET").mock(return_value=httpx.Response(404))
    settings = Settings(ha_url="http://ha.test", ha_token="x", load_model="median",
                        db_path=str(tmp_path / "carbon.db"), readiness_window_days=1)
    soc = SocMeasurement(status=SocStatus.OK, value=50, observed_at=NOW,
                         raw_state="50", reported_at=NOW, age_s=0, max_age_s=600)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        source = entities_source(settings)
        off = await current_plan(settings, rest, soc=soc, source=source)
        on = await current_plan(
            settings.model_copy(update={"carbon_region": 12}), rest, soc=soc, source=source
        )
    assert off.inputs == on.inputs
    assert off.cfg == on.cfg
    assert off.plan == on.plan
    day, forecast, guards = fixture()
    assert evaluate_readiness([day], guards, [forecast], settings) == evaluate_readiness(
        [day], guards, [forecast], settings.model_copy(update={"carbon_region": 12}),
    )
    assert all(call.request.url.host == "ha.test" for call in respx.calls)


@pytest.mark.parametrize("bad", [
    None, {}, {"data": []}, envelope([row()], 11), envelope([row()], True),
    envelope([]), envelope([None, "hostile"]),
    *[envelope([row(value=value)]) for value in
      [True, False, -1, 2001, float("nan"), float("inf"), "132", None, {}]],
    envelope([row(index="unknown")]), envelope([row(index=[])]),
    envelope([{**row(), "from": "2026-10-14T12:01Z"}]),
    envelope([{**row(), "from": "2026-10-14T12:00:01Z"}]),
    envelope([{**row(), "from": "2026-10-14T12:00:00.001Z"}]),
    envelope([{**row(), "from": "2026-10-14T12:00"}]),
    envelope([{**row(), "from": "2026-10-14T13:00+01:00"}]),
    envelope([{**row(), "to": "2026-10-14T13:00Z"}]),
    envelope([row(NOW - timedelta(days=1))]),
    envelope([row(NOW + timedelta(days=3))]),
])
@respx.mock
async def test_invalid_payload_keeps_last_good(tmp_path, bad, caplog):
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: 12)
    route = respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([row()])),
    )
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    before = await carbon_payload(path, 12, NOW)
    route.mock(return_value=httpx.Response(200, content=json.dumps(bad)))
    refresher.tick(NOW + timedelta(minutes=30), "UTC", None)
    await refresher.wait()
    assert await carbon_payload(path, 12, NOW) == before
    assert "Carbon fetch failed: invalid_payload" in caplog.text
    await refresher.close()


@respx.mock
async def test_duplicates_gaps_snapback_and_boundaries(tmp_path):
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: 12)
    midnight = NOW.replace(hour=0)
    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([
            row(midnight - timedelta(minutes=30)), row(midnight, 0),
            row(), row(NOW + timedelta(hours=1), 2000), row(value=199),
        ])),
    )
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    payload = await carbon_payload(path, 12, NOW)
    assert [r["gco2"] for r in payload["rows"]] == [0, 199, 2000]
    assert [r["estimated"] for r in payload["rows"]] == [True, False, False]
    assert (await carbon_payload(path, 12, NOW + timedelta(minutes=30)))["status"] == "unavailable"
    await refresher.close()


@respx.mock
async def test_cache_overwrite_pruning_regions_and_stale(tmp_path):
    path = str(tmp_path / "carbon.db")
    region = 12
    refresher = CarbonRefresher(path, lambda: region)
    route = respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([row(NOW - timedelta(days=3))])),
    )
    old = NOW - timedelta(days=3)
    refresher.tick(old, "UTC", None)
    await refresher.wait()
    route.mock(return_value=httpx.Response(200, json=envelope([
        row(), row(NOW + timedelta(hours=2)), row(NOW + timedelta(hours=2, minutes=30)),
    ])))
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW + timedelta(hours=2)))["status"] == "ok"
    stale = await carbon_payload(path, 12, NOW + timedelta(hours=2, seconds=1))
    assert stale["status"] == "stale"
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT count(*) FROM carbon_periods") as cursor:
            assert (await cursor.fetchone())[0] == 3
    later = NOW + timedelta(minutes=30)
    route.mock(return_value=httpx.Response(200, json=envelope([row(value=150)])))
    refresher.tick(later, "UTC", None)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["rows"][0]["gco2"] == 150
    region = 1
    route.mock(return_value=httpx.Response(200, json=envelope([row(value=10)], 1)))
    refresher.tick(later + timedelta(seconds=1), "UTC", None)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["fetched_at"] == later.isoformat()
    assert (await carbon_payload(path, 1, NOW))["fetched_at"] != later.isoformat()
    assert (await carbon_payload(path, 1, NOW))["rows"][0]["gco2"] == 10
    region = None
    refresher.tick(later + timedelta(minutes=1), "UTC", None)
    await refresher.wait()
    assert (await carbon_payload(path, region, NOW))["rows"] == []
    assert route.call_count == 4
    await refresher.close()


@respx.mock
async def test_more_than_300_rows_are_bounded(tmp_path):
    path = str(tmp_path / "carbon.db")
    # A long plan horizon deliberately exercises the validation cap.
    refresher = CarbonRefresher(path, lambda: 12)
    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([
            row(NOW + timedelta(minutes=30 * i)) for i in range(305)
        ])),
    )
    refresher.tick(NOW, "UTC", NOW + timedelta(days=7))
    await refresher.wait()
    assert len((await carbon_payload(path, 12, NOW))["rows"]) == 300
    await refresher.close()


@respx.mock
async def test_retry_once_and_failure_logs_are_fixed(tmp_path, caplog):
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: 12)
    route = respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(side_effect=[
        httpx.Response(500, json={"error": CANARY}),
        httpx.Response(200, json=envelope([row()])),
    ])
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    assert route.call_count == 2
    before = await carbon_payload(path, 12, NOW)
    for response in (
        httpx.Response(500, text=CANARY),
        httpx.Response(200, text=CANARY),
        httpx.Response(200, json={"error": CANARY}),
        httpx.ReadTimeout(CANARY),
    ):
        route.mock(side_effect=response if isinstance(response, Exception) else None,
                   return_value=response if isinstance(response, httpx.Response) else None)
        refresher.tick(refresher_time := NOW + timedelta(minutes=30 * route.call_count),
                       "UTC", None)
        await refresher.wait()
        assert await carbon_payload(path, 12, NOW) == before
    assert refresher_time > NOW
    messages = [r.getMessage() for r in caplog.records if r.name == "ha_spark.energy.carbon"]
    assert messages
    assert all(m.startswith("Carbon fetch failed:") for m in messages)
    assert CANARY not in caplog.text
    assert "https://" not in " ".join(messages)
    await refresher.close()


@respx.mock
@pytest.mark.parametrize("new_region", [None, 1])
async def test_late_response_discarded_without_blocking_tick(tmp_path, new_region):
    path = str(tmp_path / "carbon.db")
    region = 12
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=envelope([row()]))

    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(side_effect=delayed)
    refresher = CarbonRefresher(path, lambda: region)
    refresher.tick(NOW, "UTC", None)
    await asyncio.wait_for(started.wait(), 1)
    # The tick returns while the request is still outstanding.
    refresher.tick(NOW + timedelta(seconds=1), "UTC", None)
    region = new_region
    release.set()
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["status"] == "unavailable"
    await refresher.close()


@respx.mock
async def test_refresh_cadence_is_elapsed_time_across_dst(tmp_path):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/London")
    now = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    route = respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([row(now.replace(minute=30))]))
    )
    refresher = CarbonRefresher(str(tmp_path / "carbon.db"), lambda: 12)
    refresher.tick(now.astimezone(zone), "Europe/London", None)
    await refresher.wait()
    refresher.tick((now + timedelta(minutes=30)).astimezone(zone), "Europe/London", None)
    await refresher.wait()
    assert route.call_count == 2
    await refresher.close()


@pytest.mark.parametrize("now,midnight", [
    (datetime(2026, 3, 29, 12, tzinfo=UTC), "2026-03-29T00:00Z"),
    (datetime(2026, 10, 25, 12, tzinfo=UTC), "2026-10-24T23:00Z"),
])
@respx.mock
async def test_household_midnight_and_plan_horizon_range(tmp_path, now, midnight):
    end = now + timedelta(hours=36)
    route = respx.get(
        "https://api.carbonintensity.org.uk/regional/intensity/"
        f"{midnight}/{end + timedelta(hours=2):%Y-%m-%dT%H:%MZ}/regionid/12"
    ).mock(return_value=httpx.Response(200, json=envelope([row(now)])))
    refresher = CarbonRefresher(str(tmp_path / "carbon.db"), lambda: 12)
    refresher.tick(now, "Europe/London", end)
    await refresher.wait()
    assert route.call_count == 1
    await refresher.close()


@respx.mock
async def test_request_timeout_and_shutdown(tmp_path):
    started = asyncio.Event()

    async def timeout(request):
        assert request.extensions["timeout"] == dict.fromkeys(
            ["connect", "read", "write", "pool"], 20,
        )
        started.set()
        raise httpx.ReadTimeout("hostile error")

    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(side_effect=timeout)
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: 12)
    refresher.tick(NOW, "UTC", None)
    await asyncio.wait_for(started.wait(), 1)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["status"] == "unavailable"
    await refresher.close()


@respx.mock
async def test_request_lines_never_log_the_url(tmp_path, caplog):
    caplog.set_level("DEBUG")
    path = str(tmp_path / "carbon.db")
    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, json=envelope([row()])))
    refresher = CarbonRefresher(path, lambda: 12)
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["rows"]
    assert "carbonintensity" not in caplog.text
    await refresher.close()


@respx.mock
async def test_region_cleared_before_retry_or_commit_is_not_fetched_or_stored(tmp_path):
    path = str(tmp_path / "carbon.db")
    region = 12

    def first(request):
        nonlocal region
        region = None
        return httpx.Response(500)

    route = respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        side_effect=first)
    refresher = CarbonRefresher(path, lambda: region)
    refresher.tick(NOW, "UTC", None)
    await refresher.wait()
    assert route.call_count == 1

    region = 12
    calls = 0

    def current() -> int | None:
        # Current for the pre-store check, cleared by the time the write commits.
        nonlocal calls
        calls += 1
        return 12 if calls == 1 else None

    route.mock(side_effect=None, return_value=httpx.Response(200, json=envelope([row()])))
    late = CarbonRefresher(path, current)
    await late._fetch(12, NOW, NOW, NOW + timedelta(hours=1))
    assert (await carbon_payload(path, 12, NOW))["status"] == "unavailable"
    await late.close()
    await refresher.close()


@respx.mock
async def test_oversized_neso_body_is_refused_unparsed(tmp_path, caplog):
    path = str(tmp_path / "carbon.db")
    refresher = CarbonRefresher(path, lambda: 12)
    body = json.dumps(envelope([row()] * 20_000)).encode()
    assert len(body) > 1_000_000
    respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
        return_value=httpx.Response(200, content=body))
    refresher.tick(NOW, "Europe/London", None)
    await refresher.wait()
    assert (await carbon_payload(path, 12, NOW))["rows"] == []
    assert "Carbon fetch failed: oversized" in caplog.text
    await refresher.close()
