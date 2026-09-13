"""Axle event source and tariff overlay contract tests for map ticket #48."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_spark.config import ConfigError, Settings, validate_axle_tariff
from ha_spark.energy.axle import (
    AxleApiError,
    fetch_axle_event,
    parse_axle_event,
    read_axle_event,
)
from ha_spark.energy.models import FlexibilityEvent, PlannerConfig, PlannerInputs
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.sources import build_schedule
from ha_spark.energy.tariff import AxleTariffProvider, FixedTariffProvider
from ha_spark.ha.rest import HomeAssistantRest

NOW = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
HORIZON = datetime(2026, 9, 12, 23, 30, tzinfo=UTC)
AXLE = "http://axle.test"
HA = "http://ha.test/api"


def _soc() -> SocMeasurement:
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=NOW,
        value=50.0,
        raw_state="50",
        reported_at=NOW,
        age_s=0.0,
        max_age_s=600.0,
    )


def _cfg() -> PlannerConfig:
    return PlannerConfig(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=1.0,
        window_start=datetime.min.time(),
        window_end=datetime.min.time(),
    )


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "start_time": "2026-09-12T17:00:00+00:00",
        "end_time": "2026-09-12T18:00:00+00:00",
        "import_export": "export",
        "updated_at": "2026-09-12T15:55:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_parse_export_event_requires_explicit_fresh_window() -> None:
    event = parse_axle_event(_payload(), now=NOW, rate_gbp_kwh=1.0)

    assert event == FlexibilityEvent(
        start=datetime(2026, 9, 12, 17, 0, tzinfo=UTC),
        end=datetime(2026, 9, 12, 18, 0, tzinfo=UTC),
        direction="export",
        updated_at=datetime(2026, 9, 12, 15, 55, tzinfo=UTC),
        rate_gbp_kwh=1.0,
    )


def test_parse_import_event_is_ignored() -> None:
    assert parse_axle_event(_payload(import_export="import"), now=NOW, rate_gbp_kwh=1.0) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"start_time": "2026-09-12T17:00:00+00:00"},
        _payload(end_time="bad-time"),
        _payload(start_time="2026-09-12T18:00:00+00:00"),
        _payload(import_export="sideways"),
    ],
)
def test_parse_malformed_event_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises(AxleApiError):
        parse_axle_event(payload, now=NOW, rate_gbp_kwh=1.0)


def test_parse_accepts_an_event_published_hours_ago() -> None:
    """`updated_at` is a change timestamp, not a heartbeat.

    Axle moves it only when it modifies the event, and events are normally
    published around four hours ahead. Treating it as a freshness signal
    rejected every poll after the first, so export never fired.
    """
    event = parse_axle_event(
        _payload(updated_at=(NOW - timedelta(hours=4)).isoformat()),
        now=NOW,
        rate_gbp_kwh=1.0,
    )

    assert event is not None
    assert event.updated_at == NOW - timedelta(hours=4)


def test_parse_future_dated_update_fails_closed() -> None:
    with pytest.raises(AxleApiError, match="future"):
        parse_axle_event(
            _payload(updated_at=(NOW + timedelta(hours=1)).isoformat()),
            now=NOW,
            rate_gbp_kwh=1.0,
        )


def test_axle_tariff_provider_marks_only_event_slots_for_export() -> None:
    event = parse_axle_event(_payload(), now=NOW, rate_gbp_kwh=1.0)
    assert event is not None
    inputs = PlannerInputs(
        soc=_soc(),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=1.0,
        load_slots=(0.5,) * 8,
        horizon_start=datetime(2026, 9, 12, 16, 30, tzinfo=UTC),
        flexibility_event=event,
    )
    schedule = AxleTariffProvider(
        fallback=FixedTariffProvider(0.07, 0.30, 0.0),
        event_rate_gbp_kwh=1.0,
    ).schedule(inputs, _cfg())

    assert schedule.export_prices == (0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_axle_settings_expose_the_source_contract() -> None:
    settings = Settings(
        tariff_provider="axle",
        axle_api_key="secret-token",
        axle_event_rate_gbp_kwh=1.25,
    )

    assert settings.axle_api_key == "secret-token"
    assert settings.axle_event_rate_gbp_kwh == 1.25


def test_validate_axle_requires_direct_or_ha_source() -> None:
    with pytest.raises(ConfigError, match="axle_api_key or axle_event_entity"):
        validate_axle_tariff(Settings(tariff_provider="axle"))


def test_validate_axle_accepts_the_ha_mirror_without_api_credentials() -> None:
    validate_axle_tariff(
        Settings(tariff_provider="axle", axle_event_entity="sensor.axle_event")
    )


@respx.mock
async def test_fetch_axle_event_uses_bearer_auth_and_explicit_payload() -> None:
    route = respx.get(f"{AXLE}/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json=_payload())
    )

    event = await fetch_axle_event(
        Settings(axle_api_url=AXLE, axle_api_key="secret-token", axle_event_rate_gbp_kwh=1.25),
        now=NOW,
    )

    assert event is not None
    assert event.rate_gbp_kwh == 1.25
    assert route.calls[0].request.headers["authorization"] == "Bearer secret-token"
    assert route.calls[0].request.headers["accept"] == "application/json"


@respx.mock
async def test_fetch_axle_event_accepts_empty_snapshot_as_no_event() -> None:
    respx.get(f"{AXLE}/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json={})
    )

    event = await fetch_axle_event(
        Settings(axle_api_url=AXLE, axle_api_key="secret-token"),
        now=NOW,
    )

    assert event is None


@respx.mock
async def test_axle_http_event_flows_through_build_schedule() -> None:
    respx.get(f"{AXLE}/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json=_payload())
    )
    settings = Settings(
        tariff_provider="axle",
        axle_api_url=AXLE,
        axle_api_key="secret-token",
        axle_event_rate_gbp_kwh=1.25,
    )
    event = await fetch_axle_event(settings, now=NOW)
    inputs = PlannerInputs(
        soc=_soc(),
        solar_tomorrow_kwh=0.0,
        predicted_home_load_kwh=1.0,
        load_slots=(0.5,) * 8,
        horizon_start=datetime(2026, 9, 12, 16, 30, tzinfo=UTC),
        flexibility_event=event,
    )

    schedule = build_schedule(settings, inputs, _cfg())

    assert schedule.export_prices == (0.0, 1.25, 1.25, 0.0, 0.0, 0.0, 0.0, 0.0)


@respx.mock
async def test_read_axle_event_uses_ha_mirror_when_api_fails() -> None:
    respx.get(f"{AXLE}/vpp/home-assistant/event").mock(return_value=httpx.Response(503))
    respx.get(f"{HA}/states/sensor.axle_event").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.axle_event",
                "state": _payload()["start_time"],
                "attributes": _payload(),
            },
        )
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="ha-token",
        axle_api_url=AXLE,
        axle_api_key="secret-token",
        axle_event_entity="sensor.axle_event",
    )

    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        event = await read_axle_event(settings, rest, now=NOW)

    assert event is not None
    assert event.start == datetime(2026, 9, 12, 17, 0, tzinfo=UTC)


@respx.mock
async def test_read_axle_event_does_not_turn_malformed_api_data_into_cancellation() -> None:
    respx.get(f"{AXLE}/vpp/home-assistant/event").mock(
        return_value=httpx.Response(200, json={"start_time": _payload()["start_time"]})
    )

    with pytest.raises(AxleApiError):
        await fetch_axle_event(
            Settings(axle_api_url=AXLE, axle_api_key="secret-token"),
            now=NOW,
        )


@respx.mock
async def test_read_axle_event_treats_unavailable_ha_state_as_failure() -> None:
    respx.get(f"{HA}/states/sensor.axle_event").mock(
        return_value=httpx.Response(
            200,
            json={
                "entity_id": "sensor.axle_event",
                "state": "unavailable",
                "attributes": {},
            },
        )
    )
    settings = Settings(
        ha_url="http://ha.test",
        ha_token="ha-token",
        axle_event_entity="sensor.axle_event",
    )

    with pytest.raises(AxleApiError):
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            await read_axle_event(settings, rest, now=NOW)
