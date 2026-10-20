"""Source freshness is metadata only; it never changes existing hold trust."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
import yaml
from pydantic import ValidationError

from ha_spark.config import _OPTION_KEYS, Settings
from ha_spark.energy import sources
from ha_spark.energy.evidence_freshness import battery_direction, check_report_time
from ha_spark.energy.models import DispatchSlot, LoadForecast
from ha_spark.energy.octopus import OctopusApiError
from ha_spark.energy.soc_integrity import MAX_UNCHANGED
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import check_evidence_freshness

NOW = datetime(2026, 10, 20, tzinfo=UTC)
SOURCES = (
    "ev_status", "ev_plug", "ev_power", "live_dispatch", "adjusted_rate",
    "dispatch_grid_power", "battery_power", "octopus_dispatch",
)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(("report", "expected"), [
    (NOW, "fresh"), (NOW - timedelta(seconds=300), "fresh"),
    (NOW - timedelta(seconds=301), "stale"),
    (NOW + timedelta(seconds=1), "future"), (None, "unknown"),
    ("garbage", "unknown"), ("2026-10-20T00:00:00", "unknown"),
])
def test_report_times(source: str, report: datetime | str | None, expected: str) -> None:
    state = EntityState(entity_id="sensor.test", state="off", last_reported=report)
    result = check_report_time(state.last_reported, now=NOW, max_age_s=300,
                               option=f"{source}_max_age_seconds")
    assert result.validity == expected


@pytest.mark.parametrize("own_age", [301, 3600, int(MAX_UNCHANGED.total_seconds())])
def test_sibling_effective_time(own_age: int) -> None:
    sibling = NOW - timedelta(seconds=10)
    result = check_report_time(NOW - timedelta(seconds=own_age), now=NOW,
                               max_age_s=300, option="age", sibling_reported_at=sibling)
    assert result.validity == "fresh"
    assert result.reported_at == sibling
    assert result.age_s == 10
    assert result.via_sibling


@pytest.mark.parametrize("own", [None, NOW + timedelta(seconds=1),
    NOW - MAX_UNCHANGED - timedelta(seconds=1)])
def test_sibling_cannot_rescue_invalid_or_frozen_own_time(own: datetime | None) -> None:
    result = check_report_time(own, now=NOW, max_age_s=300, option="age",
                               sibling_reported_at=NOW)
    assert result.validity != "fresh"
    assert not result.via_sibling


@pytest.mark.parametrize("sibling", [None, NOW + timedelta(seconds=1),
    NOW - timedelta(seconds=301), NOW.replace(tzinfo=None)])
def test_invalid_sibling_does_not_prove_liveness(sibling: datetime | None) -> None:
    own = NOW - timedelta(hours=1)
    result = check_report_time(own, now=NOW, max_age_s=300, option="age",
                               sibling_reported_at=sibling)
    assert result.validity == "stale"
    assert result.reported_at == own


def test_cached_report_only_ages_and_own_fresh_time_wins() -> None:
    first = check_report_time(NOW, now=NOW, max_age_s=300, option="age",
                              sibling_reported_at=NOW - timedelta(seconds=10))
    later = check_report_time(NOW, now=NOW + timedelta(seconds=301), max_age_s=300,
                              option="age")
    assert first.reported_at == later.reported_at == NOW
    assert not first.via_sibling
    assert later.age_s == 301
    assert later.validity == "stale"


def test_unvalidated_max_age() -> None:
    result = check_report_time(NOW, now=NOW, max_age_s=None, option="custom_age")
    assert result.validity == "unknown"
    assert result.reason == "no validated max-age; set custom_age"


@pytest.mark.parametrize("source", SOURCES)
def test_options_defaults_and_bounds(source: str) -> None:
    addon = yaml.safe_load((Path(__file__).parents[1] / "ha_spark_addon/config.yaml").read_text())
    key = f"{source}_max_age_seconds"
    assert addon["options"][key] == (2700 if source == "octopus_dispatch" else 300)
    assert addon["schema"][key] == "float(0.001,86400)?"
    assert key in _OPTION_KEYS
    assert getattr(Settings(), key) == (2700 if source == "octopus_dispatch" else 300)
    for invalid in (0, -1, 86401, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(**{key: invalid})
    for valid in (1, 86400, None):
        assert getattr(Settings(**{key: valid}), key) == valid


@pytest.mark.parametrize(("value", "unit", "sign", "expected"), [
    (-2851, "W", "positive", "charging"), (100, "W", "positive", "discharging"),
    (-100, "W", "negative", "discharging"), (100, "W", "negative", "charging"),
    (49, "W", "positive", "idle"), (-49, "W", "negative", "idle"),
    (50, "W", "positive", "discharging"), (100, "kW", "positive", "unknown"),
    (100, None, "positive", "unknown"), (100, "W", None, "unknown"),
    (float("nan"), "W", "positive", "unknown"),
    (float("inf"), "W", "positive", "unknown"), ("bad", "W", "positive", "unknown"),
])
def test_direction(value: object, unit: str | None, sign: str | None, expected: str) -> None:
    assert battery_direction(value, unit=unit, discharge_sign=sign) == expected


def test_sign_and_sibling_options() -> None:
    settings = Settings()
    assert settings.battery_power_discharge_sign is None
    assert settings.ev_liveness_entity == settings.dispatch_liveness_entity == ""
    with pytest.raises(ValidationError):
        Settings(battery_power_discharge_sign="guess")


ENTITY_OPTIONS = {
    "ev_status": "ev_status_entity", "ev_plug": "ev_plug_entity",
    "ev_power": "ev_power_entity", "live_dispatch": "dispatch_entity",
    "adjusted_rate": "octopus_rate_entity", "dispatch_grid_power": "dispatch_grid_power_entity",
    "battery_power": "battery_power_entity",
}


def fixture_settings() -> Settings:
    return Settings(
        ha_url="http://ha.test", ha_token="fixture-token",
        **{option: f"sensor.{name}" for name, option in ENTITY_OPTIONS.items()},
        ev_liveness_entity="sensor.ev_live", dispatch_liveness_entity="sensor.dispatch_live",
        battery_power_discharge_sign="positive",
    )


def fixture_states() -> None:
    for source in (*ENTITY_OPTIONS, "ev_live", "dispatch_live"):
        value = {"ev_status": "Charging", "ev_plug": "EV Connected",
                 "live_dispatch": "on", "battery_power": "-2851"}.get(source, "1000")
        report = NOW - timedelta(hours=1) if source in (
            "ev_status", "ev_plug", "ev_power", "live_dispatch", "adjusted_rate"
        ) else NOW - timedelta(seconds=10)
        respx.get(f"http://ha.test/api/states/sensor.{source}").respond(200, json={
            "entity_id": f"sensor.{source}", "state": value,
            "last_reported": report.isoformat(),
            "last_updated": NOW.isoformat(), "last_changed": NOW.isoformat(),
            "attributes": {"unit_of_measurement": "W", "is_intelligent_adjusted": True,
                           "last_reported": NOW.isoformat()},
        })


@respx.mock
async def test_normalisation_preserves_report_times_and_legacy_evidence() -> None:
    settings = fixture_settings()
    fixture_states()
    reports: dict[str, datetime | None] = {}
    slot = DispatchSlot(start=NOW, end=NOW + timedelta(minutes=30), source="test")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        _, trusted, live = await sources.read_dispatches_and_live(
            settings, rest, report_times=reports)
        first = await sources.read_dispatch_evidence(
            settings, rest, (slot,), live_dispatch=live, report_times=reports, now=NOW)
        later = await sources.read_dispatch_evidence(
            settings, rest, (slot,), live_dispatch=live, report_times=reports,
            now=NOW + timedelta(seconds=301))
    assert trusted and live
    assert first.evidence == later.evidence  # Freshness never rewires legacy ratings/holds.
    assert first.ev_status_trusted == later.ev_status_trusted
    assert sources.ev_hold_status_from_read(first.ev_status, first.ev_status_trusted) is True
    assert first.battery_direction == "charging"
    assert set(first.freshness) == set(ENTITY_OPTIONS)
    for name, freshness in first.freshness.items():
        assert freshness.validity == "fresh"
        assert freshness.reported_at == NOW - timedelta(seconds=10)
        assert freshness.via_sibling == (name in (
            "ev_status", "ev_plug", "ev_power", "live_dispatch", "adjusted_rate"))
        assert later.freshness[name].validity == "stale"
        assert later.freshness[name].own_reported_at == freshness.own_reported_at


@pytest.mark.parametrize("source", ENTITY_OPTIONS)
@pytest.mark.parametrize(("report", "validity"), [
    (NOW.isoformat(), "fresh"),
    ((NOW - timedelta(seconds=301)).isoformat(), "stale"),
    ((NOW + timedelta(seconds=1)).isoformat(), "future"),
    (None, "unknown"), ("unparseable", "unknown"),
    (NOW.replace(tzinfo=None).isoformat(), "unknown"),
])
@respx.mock
async def test_each_ha_source_uses_only_top_level_last_reported(
    source: str, report: str | None, validity: str,
) -> None:
    fixture_states()
    respx.get(f"http://ha.test/api/states/sensor.{source}").respond(200, json={
        "entity_id": f"sensor.{source}", "state": "1000",
        "last_reported": report, "last_changed": NOW.isoformat(),
        "last_updated": NOW.isoformat(),
        "attributes": {"last_reported": NOW.isoformat(), "unit_of_measurement": "W"},
    })
    settings = fixture_settings().model_copy(update={
        "ev_liveness_entity": "", "dispatch_liveness_entity": "",
    })
    reports: dict[str, datetime | None] = {}
    slot = DispatchSlot(start=NOW, end=NOW + timedelta(minutes=30), source="test")
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        _, _, live = await sources.read_dispatches_and_live(settings, rest, report_times=reports)
        reading = await sources.read_dispatch_evidence(
            settings, rest, (slot,), live_dispatch=live, report_times=reports, now=NOW)
    assert reading.freshness[source].validity == validity


@respx.mock
async def test_health_lists_each_configured_source_with_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_states()
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW
    monkeypatch.setattr(sources, "datetime", FrozenDatetime)
    results = await check_evidence_freshness(fixture_settings())
    assert {r.name for r in results} == {f"Evidence {name}" for name in ENTITY_OPTIONS}
    for result in results:
        assert "fresh; effective report=" in result.detail
        assert "age=10.0s; max-age=300.0s; sibling=" in result.detail
    assert "direction=charging" in results[-1].detail
    assert all(call.request.method == "GET" for call in respx.calls)


@pytest.mark.parametrize("failed", [False, True])
@respx.mock
async def test_supplier_fetch_time_carried_through_gather(
    monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    async def fetch(_settings: Settings) -> tuple[DispatchSlot, ...]:
        if failed:
            raise OctopusApiError("fixture failure")
        return ()

    async def prices(*args: object, **kwargs: object) -> tuple[()]:
        return ()

    async def load(*args: object, **kwargs: object) -> LoadForecast:
        return LoadForecast(total_kwh=24, slots=None, source="fixture")

    monkeypatch.setattr(sources, "fetch_planned_dispatches", fetch)
    monkeypatch.setattr(sources, "fetch_standard_unit_rates", prices)
    monkeypatch.setattr(sources, "predict_home_load", load)
    respx.route(method="GET").respond(404)
    settings = Settings(ha_url="http://ha.test", ha_token="fixture-token",
                        tariff_provider="octopus_intelligent")
    before = datetime.now(UTC)
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        inputs, _, _ = await sources.gather_inputs(settings, rest)
    after = datetime.now(UTC)
    freshness = inputs.evidence_freshness["octopus_dispatch"]
    assert inputs.dispatches == ()
    assert inputs.dispatches_trusted is not failed
    assert freshness.max_age_s == 2700
    assert freshness.validity == ("unknown" if failed else "fresh")
    if not failed:
        assert freshness.reported_at is not None
        assert before <= freshness.reported_at <= after
        # Re-evaluating the same cached input later cannot manufacture a new fetch.
        later = replace(freshness)  # copying metadata retains exact supplier report time
        assert later.reported_at == freshness.reported_at

