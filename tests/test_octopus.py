"""Tests for Octopus CSV parsing and the consumption API client."""

from __future__ import annotations

import base64
import json
import logging
import traceback
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy import octopus
from ha_spark.energy.octopus import (
    OctopusApiError,
    discover_import_tariff,
    fetch_agreement_rates,
    fetch_consumption,
    fetch_planned_dispatches,
    fetch_standard_unit_rates,
    parse_octopus_csv,
)
from tests.octopus_helpers import agreement_failure, agreement_payload, token_response

API = "http://octo.test/v1"


def _free_payload(enrolled: Any = True, edges: Any = None) -> dict[str, Any]:
    return {"data": {
        "isEnrolledInCustomerFlexibilityCampaign": enrolled,
        "customerFlexibilityCampaignEvents": {"edges": edges if edges is not None else [
            {"node": {"code": "event-private", "startAt": "2025-10-25T11:00:00+00:00",
                      "endAt": "2025-10-25T14:00:00+00:00", "isEventParticipant": False}},
        ]},
    }}


@pytest.mark.parametrize("enrolled, state", [
    (True, "sessions"), (False, "not_enrolled"), (None, "not_enrolled"),
])
@respx.mock
async def test_free_electricity_eligibility(enrolled: Any, state: str) -> None:
    route = respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=_free_payload(enrolled)),
    ])
    now = datetime(2026, 11, 12, tzinfo=UTC)
    result = await octopus.fetch_free_electricity_sessions(
        _dispatch_settings(octopus_mpan="mpan-private"), now=now,
    )
    assert result.state == state
    assert result.read_at == now
    if enrolled:
        assert result.sessions[0].id == "event-private"
        assert result.sessions[0].end < now  # Past events remain readable.
    else:
        assert result.sessions == ()
    body = json.loads(route.calls[1].request.content)
    assert body["variables"] == {"accountNumber": "A-1234ABCD", "mpan": "mpan-private"}
    assert 'campaignSlug: "free_electricity"' in body["query"]
    assert "first: 10" in body["query"]
    assert "mutation" not in body["query"]


@respx.mock
async def test_free_electricity_times_null_edges_and_shared_token() -> None:
    edges = [
        {"node": {"code": "later", "startAt": "2026-11-13T15:00:00+01:00",
                  "endAt": "2026-11-13T16:00:00+01:00"}},
        None, {"node": None},
        {"node": {"code": "earlier", "startAt": "2026-11-12T11:00:00Z",
                  "endAt": "2026-11-12T12:00:00Z"}},
    ]
    route = respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=_free_payload(edges=edges)),
        httpx.Response(200, json=agreement_payload((6.6, 36.0))),
    ])
    settings = _dispatch_settings(octopus_mpan="configured")
    result = await octopus.fetch_free_electricity_sessions(settings)
    assert result.state == "sessions"
    assert [session.id for session in result.sessions] == ["earlier", "later"]
    assert result.sessions[1].start == datetime(2026, 11, 13, 14, tzinfo=UTC)
    await fetch_agreement_rates(settings)
    assert route.call_count == 3  # Both readers share the same token.


async def test_free_electricity_without_credentials() -> None:
    result = await octopus.fetch_free_electricity_sessions(Settings())
    assert result.state == "failed"
    assert result.reason == "not_configured"


@pytest.mark.parametrize("failure", [
    "http", "graphql", "shape", "no_data", "errors_shape", "truthy",
    "top_list", "top_null", "top_number", "top_text", "data_null", "data_list",
])
@respx.mock
async def test_free_electricity_failed_reads(
    failure: str, caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _free_payload()
    if failure == "graphql":
        payload["errors"] = [{"message": "jwt-private key-private account-private mpan-private"}]
    elif failure == "shape":
        payload["data"]["customerFlexibilityCampaignEvents"]["edges"] = {}
    elif failure == "no_data":
        del payload["data"]["isEnrolledInCustomerFlexibilityCampaign"]
    elif failure == "errors_shape":
        payload["errors"] = True
    elif failure == "truthy":
        payload["data"]["isEnrolledInCustomerFlexibilityCampaign"] = "true"
    odd: dict[str, Any] = {
        "top_list": [], "top_null": None, "top_number": 5, "top_text": "text",
        "data_null": {"data": None}, "data_list": {"data": []},
    }
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(),
        httpx.Response(503 if failure == "http" else 200, json=odd.get(failure, payload)),
    ])
    result = await octopus.fetch_free_electricity_sessions(_dispatch_settings(
        octopus_api_key="key-private", octopus_account_number="account-private",
        octopus_mpan="mpan-private",
    ))
    assert result.state == "failed"
    assert result.reason
    assert result.sessions == ()
    for secret in ("jwt-private", "key-private", "account-private", "mpan-private"):
        assert secret not in repr(result) + caplog.text


@respx.mock
async def test_free_electricity_bad_event_is_dropped_not_guessed() -> None:
    good = {"code": "good", "startAt": "2026-11-12T11:00:00Z", "endAt": "2026-11-12T12:00:00Z",
            "isEventParticipant": False}
    edges = [
        {"node": good}, 7, {"cursor": "x"}, {"node": "text"},
        {"node": {**good, "code": "no-start", "startAt": None}},
        {"node": {k: v for k, v in good.items() if k != "endAt"} | {"code": "no-end"}},
        {"node": {**good, "code": "bad-time", "startAt": "bad"}},
        {"node": {**good, "code": "number", "startAt": 1794481200, "endAt": 1794484800}},
        {"node": {**good, "code": "digits", "startAt": "1794481200", "endAt": "1794484800"}},
        {"node": {**good, "code": "year-one", "startAt": "0001-01-01T00:00:00+14:00",
                  "endAt": "0001-01-02T00:00:00+14:00"}},
        {"node": {**good, "code": "naive", "endAt": "2026-11-12T12:00:00"}},
        {"node": {**good, "code": "reversed", "endAt": "2026-11-12T10:00:00Z"}},
        {"node": {**good, "code": "empty", "endAt": "2026-11-12T11:00:00Z"}},
        {"node": {**good, "code": ""}},
        {"node": {**good, "code": "joined", "isEventParticipant": True}},
    ]
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=_free_payload(edges=edges)),
        httpx.Response(200, json=_free_payload(edges=edges[1:-1])),
    ])
    settings = _dispatch_settings(octopus_mpan="configured")
    result = await octopus.fetch_free_electricity_sessions(settings)
    assert result.state == "sessions"
    assert [session.id for session in result.sessions] == ["good", "joined"]
    result = await octopus.fetch_free_electricity_sessions(settings)
    assert (result.state, result.sessions) == ("no_session", ())


@respx.mock
async def test_free_electricity_discovered_mpan_and_missing() -> None:
    account = respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": [
            {"mpan": "discovered", "is_export": False, "agreements": []},
        ]}],
    }))
    route = respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=_free_payload(edges=[])),
    ])
    result = await octopus.fetch_free_electricity_sessions(_dispatch_settings())
    assert result.state == "no_session"
    assert json.loads(route.calls[1].request.content)["variables"]["mpan"] == "discovered"
    account.mock(return_value=httpx.Response(200, json={"properties": []}))
    result = await octopus.fetch_free_electricity_sessions(_dispatch_settings())
    assert result.state == "failed"
    assert result.reason == "supply_point_unavailable"


@pytest.mark.parametrize("mpan, reason", [
    ("", "supply_point_unavailable"), ("configured", "read_error"),
])
async def test_free_electricity_malformed_endpoint_is_a_failed_read(mpan: str, reason: str) -> None:
    result = await octopus.fetch_free_electricity_sessions(
        _dispatch_settings(octopus_api_url="http://[::1/v1", octopus_mpan=mpan),
    )
    assert (result.state, result.reason, result.sessions) == ("failed", reason, ())


@pytest.mark.parametrize("values", [(6.6, 6.6, 36.2828, 36.2828), (12.0,)])
@respx.mock
async def test_agreement_rates(values: tuple[float, ...]) -> None:
    route = respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=agreement_payload(values)),
    ])
    rates = await fetch_agreement_rates(_dispatch_settings())
    assert rates.offpeak == pytest.approx(min(values) / 100)
    assert rates.peak == pytest.approx(max(values) / 100)
    request = route.calls[1].request
    body = json.loads(request.content)
    assert body["variables"] == {"account": "A-1234ABCD"}
    assert "A-1234ABCD" not in body["query"]
    assert request.headers["Authorization"] == "JWT jwt-private"
    assert "jwt-private" not in request.content.decode()


@pytest.mark.parametrize("reason", ["http", "errors", "shape", "three", "negative"])
@respx.mock
async def test_agreement_failure_is_sanitised(reason: str) -> None:
    respx.post(f"{API}/graphql/").mock(side_effect=[token_response(), agreement_failure(reason)])
    with pytest.raises(OctopusApiError) as error:
        await fetch_agreement_rates(_dispatch_settings())
    assert "sk_test" not in str(error.value)
    assert "jwt-private" not in str(error.value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 200.01])
@respx.mock
async def test_agreement_rejects_unsafe_rates(value: float) -> None:
    payload = agreement_payload()
    units = payload["data"]["account"]["electricityAgreements"][0]["tariff"]["unitRates"]
    units[0]["value"] = value
    # Use raw JSON to exercise non-finite numbers, not just invalid strings.
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, content=json.dumps(payload)),
    ])
    with pytest.raises(OctopusApiError):
        await fetch_agreement_rates(_dispatch_settings())


@pytest.mark.parametrize("change", ["expired", "future", "export", "multiple", "empty"])
@respx.mock
async def test_agreement_filters_current_imports(change: str) -> None:
    payload = agreement_payload()
    agreements = payload["data"]["account"]["electricityAgreements"]
    agreement = agreements[0]
    if change == "expired":
        agreement["validTo"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    elif change == "future":
        agreement["validFrom"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    elif change == "export":
        agreement["meterPoint"]["direction"] = "EXPORT"
    elif change == "multiple":
        agreements.append(agreement.copy())
    else:
        agreement["tariff"]["unitRates"] = []
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=payload),
    ])
    with pytest.raises(OctopusApiError):
        await fetch_agreement_rates(_dispatch_settings())


@pytest.mark.parametrize("tariff, expected", [
    ({"__typename": "DayNightTariff", "dayRate": 30.0, "nightRate": 7.0}, (0.07, 0.30)),
    ({"__typename": "StandardTariff", "unitRate": 20.0}, (0.20, 0.20)),
    ({"__typename": "ThreeRateTariff", "dayRate": 30.0, "nightRate": 7.0,
      "offPeakRate": 7.0}, (0.07, 0.30)),
])
@respx.mock
async def test_agreement_other_tariff_shapes(
    tariff: dict[str, object], expected: tuple[float, float],
) -> None:
    payload = agreement_payload()
    payload["data"]["account"]["electricityAgreements"][0]["tariff"] = tariff
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=payload),
    ])
    rates = await fetch_agreement_rates(_dispatch_settings())
    assert (rates.offpeak, rates.peak) == pytest.approx(expected)


@pytest.mark.parametrize("auth", [[], {"data": []}, {"errors": ["sk_test jwt-private"]}])
@respx.mock
async def test_agreement_bad_auth_degrades(auth: object) -> None:
    respx.post(f"{API}/graphql/").mock(return_value=httpx.Response(200, json=auth))
    with pytest.raises(OctopusApiError) as error:
        await fetch_agreement_rates(_dispatch_settings())
    assert "sk_test" not in str(error.value)
    assert "jwt-private" not in str(error.value)


@respx.mock
async def test_agreement_ignores_noncurrent_unit_rates_and_export() -> None:
    payload = agreement_payload()
    agreements = payload["data"]["account"]["electricityAgreements"]
    agreements.append({**agreements[0], "meterPoint": {"direction": "EXPORT"}})
    units = agreements[0]["tariff"]["unitRates"]
    units.extend([
        {"value": 99.0, "validFrom": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
         "validTo": None},
        {"value": 98.0, "validFrom": "2020-01-01T00:00:00Z",
         "validTo": "2020-01-02T00:00:00Z"},
    ])
    respx.post(f"{API}/graphql/").mock(side_effect=[
        token_response(), httpx.Response(200, json=payload),
    ])
    rates = await fetch_agreement_rates(_dispatch_settings())
    assert rates.offpeak == pytest.approx(0.066)
    assert rates.peak == pytest.approx(0.362828)


def _settings(api_key: str = "sk_test") -> Settings:
    return Settings(
        octopus_api_url=API,
        octopus_api_key=api_key,
        octopus_mpan="1000000000001",
        octopus_meter_serial="00A0000000",
    )


def test_csv_parses_standard_export() -> None:
    text = (
        " Consumption (kwh), Start, End\n"
        "0.123,2026-06-01T00:00:00+01:00,2026-06-01T00:30:00+01:00\n"
        "0.456,2026-06-01T00:30:00+01:00,2026-06-01T01:00:00+01:00\n"
    )
    intervals = parse_octopus_csv(text)
    assert len(intervals) == 2
    assert intervals[0].kwh == 0.123
    # BST timestamps normalize to UTC.
    assert intervals[0].start == datetime(2026, 5, 31, 23, 0, tzinfo=UTC)
    assert intervals[0].start.tzinfo == UTC


def test_csv_tolerates_header_variants_and_bom() -> None:
    text = (
        "﻿Start,END,Consumption (kWh)\n"
        "2026-06-01T00:00:00Z,2026-06-01T00:30:00Z,0.5\n"
    )
    intervals = parse_octopus_csv(text)
    assert len(intervals) == 1
    assert intervals[0].kwh == 0.5
    assert intervals[0].start == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def test_csv_skips_bad_rows() -> None:
    text = (
        "Consumption (kwh),Start,End\n"
        "not-a-number,2026-06-01T00:00:00Z,2026-06-01T00:30:00Z\n"
        "0.5,garbage,2026-06-01T01:00:00Z\n"
        "0.7,2026-06-01T01:00:00Z,2026-06-01T01:30:00Z\n"
    )
    intervals = parse_octopus_csv(text)
    assert len(intervals) == 1
    assert intervals[0].kwh == 0.7


def test_csv_rejects_unrecognized_header() -> None:
    with pytest.raises(ValueError, match="Unrecognized"):
        parse_octopus_csv("foo,bar\n1,2\n")


@respx.mock
async def test_api_follows_pagination_and_authenticates() -> None:
    url = f"{API}/electricity-meter-points/1000000000001/meters/00A0000000/consumption/"
    route = respx.get(url__startswith=url).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "next": f"{url}?page=2",
                    "results": [
                        {
                            "consumption": 0.25,
                            "interval_start": "2026-06-01T00:00:00Z",
                            "interval_end": "2026-06-01T00:30:00Z",
                        }
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "next": None,
                    "results": [
                        {
                            "consumption": 0.5,
                            "interval_start": "2026-06-01T00:30:00+00:00",
                            "interval_end": "2026-06-01T01:00:00+00:00",
                        }
                    ],
                },
            ),
        ]
    )

    intervals = await fetch_consumption(
        _settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert [i.kwh for i in intervals] == [0.25, 0.5]
    assert route.call_count == 2
    assert "page=2" in str(route.calls[1].request.url)
    auth_header = route.calls[0].request.headers["authorization"]
    assert auth_header.startswith("Basic ")


@respx.mock
async def test_api_error_on_http_failure() -> None:
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(401))
    with pytest.raises(OctopusApiError, match="request failed"):
        await fetch_consumption(_settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC))


async def test_api_error_when_unconfigured() -> None:
    with pytest.raises(OctopusApiError, match="not configured"):
        await fetch_consumption(
            _settings(api_key=""), period_from=datetime(2026, 6, 1, tzinfo=UTC)
        )


# --- standard-unit-rates (P8.5, #39) ---


def _rates_settings(**kw: str) -> Settings:
    base = dict(
        octopus_api_url=API, octopus_api_key="sk_test",
        octopus_product_code="INTELLI-VAR-22-10-14",
        octopus_tariff_code="E-1R-INTELLI-VAR-22-10-14-A",
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@respx.mock
async def test_rates_follows_pagination_and_authenticates() -> None:
    url = (
        f"{API}/products/INTELLI-VAR-22-10-14/electricity-tariffs/"
        "E-1R-INTELLI-VAR-22-10-14-A/standard-unit-rates/"
    )
    route = respx.get(url__startswith=url).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "next": f"{url}?page=2",
                    "results": [
                        {"valid_from": "2026-06-01T00:00:00Z", "valid_to": "2026-06-01T00:30:00Z",
                         "value_inc_vat": 0.12}
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "next": None,
                    "results": [
                        {"valid_from": "2026-06-01T00:30:00Z", "valid_to": "2026-06-01T01:00:00Z",
                         "value_inc_vat": 0.09}
                    ],
                },
            ),
        ]
    )
    points = await fetch_standard_unit_rates(
        _rates_settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert [p.price for p in points] == [0.12, 0.09]
    assert route.call_count == 2
    auth_header = route.calls[0].request.headers["authorization"]
    assert auth_header.startswith("Basic ")


@respx.mock
async def test_rates_tolerates_malformed_results() -> None:
    url = (
        f"{API}/products/INTELLI-VAR-22-10-14/electricity-tariffs/"
        "E-1R-INTELLI-VAR-22-10-14-A/standard-unit-rates/"
    )
    respx.get(url__startswith=url).mock(
        return_value=httpx.Response(
            200,
            json={"next": None, "results": ["not-a-dict", {"valid_from": "bad"}]},
        )
    )
    points = await fetch_standard_unit_rates(
        _rates_settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert points == ()


@respx.mock
async def test_rates_error_on_http_failure() -> None:
    respx.get(url__regex=r".*").mock(return_value=httpx.Response(401))
    with pytest.raises(OctopusApiError, match="request failed"):
        await fetch_standard_unit_rates(
            _rates_settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC)
        )


async def test_rates_error_when_unconfigured() -> None:
    with pytest.raises(OctopusApiError, match="not configured"):
        await fetch_standard_unit_rates(
            _rates_settings(octopus_product_code=""), period_from=datetime(2026, 6, 1, tzinfo=UTC)
        )


@respx.mock
async def test_discovery_uses_current_import_agreement() -> None:
    now = datetime.now(UTC)
    account = respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": [
            {"mpan": "export-id", "is_export": True, "agreements": [{
                "tariff_code": "E-1R-EXPORT-A", "valid_from": (now - timedelta(days=1)).isoformat(),
                "valid_to": None}]},
            {"mpan": "import-id", "is_export": False, "agreements": [
                {"tariff_code": "E-1R-OLD-A", "valid_from": (now - timedelta(days=2)).isoformat(),
                 "valid_to": (now - timedelta(days=1)).isoformat()},
                {"tariff_code": "E-1R-INTELLI-VAR-24-10-29-C",
                 "valid_from": (now - timedelta(days=1)).isoformat(), "valid_to": None},
            ]},
        ]}],
    }))
    result = await discover_import_tariff(_dispatch_settings())
    assert result == ("INTELLI-VAR-24-10-29", "E-1R-INTELLI-VAR-24-10-29-C")
    assert account.called


@respx.mock
async def test_rates_discover_codes_on_each_fetch() -> None:
    now = datetime.now(UTC)
    account = respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": [{
            "mpan": "import-id", "is_export": False, "agreements": [{
                "tariff_code": "E-1R-INTELLI-VAR-24-10-29-C",
                "valid_from": (now - timedelta(days=1)).isoformat(), "valid_to": None,
            }],
        }]}],
    }))
    rates = respx.get(
        f"{API}/products/INTELLI-VAR-24-10-29/electricity-tariffs/"
        "E-1R-INTELLI-VAR-24-10-29-C/standard-unit-rates/"
    ).mock(return_value=httpx.Response(200, json={"next": None, "results": [{
        "valid_from": "2026-06-01T00:00:00Z", "valid_to": "2026-06-01T00:30:00Z",
        "value_inc_vat": 0.12,
    }]}))
    settings = _dispatch_settings()
    for _ in range(2):
        points = await fetch_standard_unit_rates(settings, period_from=now)
        assert [point.price for point in points] == [0.12]
    assert account.call_count == 2
    assert rates.call_count == 2


@pytest.mark.parametrize(
    ("points", "mpan", "expected"),
    [
        (["export"], "", "0 eligible import"),
        (["expired"], "", "0 current agreements"),
        (["import", "import2"], "", "multiple import meter points"),
        (["import", "import2"], "missing", "matches no import"),
        (["malformed"], "", "invalid shape"),
    ],
)
@respx.mock
async def test_discovery_fails_closed_without_exposing_identifiers(
    points: list[str], mpan: str, expected: str
) -> None:
    now = datetime.now(UTC)
    meters = []
    for kind in points:
        meters.append({
            "mpan": kind, "is_export": kind == "export", "agreements": [{
                "tariff_code": "bad" if kind == "malformed" else "E-1R-INTELLI-VAR-24-10-29-C",
                "valid_from": (now - timedelta(days=2)).isoformat(),
                "valid_to": (now - timedelta(days=1)).isoformat() if kind == "expired" else None,
            }],
        })
    respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": meters}],
    }))
    with pytest.raises(OctopusApiError, match=expected) as error:
        await fetch_standard_unit_rates(
            _dispatch_settings(octopus_mpan=mpan), period_from=now
        )
    assert all(value not in str(error.value) for value in ("A-1234ABCD", "import2", "missing"))


@respx.mock
async def test_discovery_selects_configured_mpan_among_imports() -> None:
    now = datetime.now(UTC)
    respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": [
            {"mpan": mpan, "is_export": False, "agreements": [{
                "tariff_code": code, "valid_from": (now - timedelta(days=1)).isoformat(),
                "valid_to": None,
            }]}
            for mpan, code in (("first", "E-1R-FIRST-A"), ("second", "E-1R-SECOND-B"))
        ]}],
    }))
    assert await discover_import_tariff(_dispatch_settings(octopus_mpan="second")) == (
        "SECOND", "E-1R-SECOND-B"
    )


@respx.mock
async def test_discovery_http_and_validation_failures_are_sanitized() -> None:
    route = respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(401))
    for response in (httpx.Response(401), httpx.Response(200, json={"properties": "bad"})):
        route.mock(return_value=response)
        with pytest.raises(OctopusApiError, match="discovery failed") as error:
            await discover_import_tariff(_dispatch_settings())
        assert "A-1234ABCD" not in str(error.value)


@respx.mock
async def test_discovery_rejects_coerced_export_flag() -> None:
    now = datetime.now(UTC)
    respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(200, json={
        "properties": [{"electricity_meter_points": [{
            "mpan": "private-id", "is_export": "false", "agreements": [{
                "tariff_code": "E-1R-INTELLI-A",
                "valid_from": (now - timedelta(days=1)).isoformat(), "valid_to": None,
            }],
        }]}],
    }))
    with pytest.raises(OctopusApiError, match="payload validation error"):
        await discover_import_tariff(_dispatch_settings())


@respx.mock
async def test_manual_rate_codes_skip_account_discovery() -> None:
    account = respx.get(f"{API}/accounts/A-1234ABCD/").mock(return_value=httpx.Response(500))
    rates = respx.get(url__startswith=f"{API}/products/INTELLI-VAR-22-10-14/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": []})
    )
    await fetch_standard_unit_rates(
        _rates_settings(octopus_account_number="A-1234ABCD"),
        period_from=datetime.now(UTC),
    )
    assert not account.called
    assert rates.called


@respx.mock
async def test_rates_refuse_pagination_outside_configured_octopus_api() -> None:
    respx.get(url__startswith=f"{API}/products/").mock(return_value=httpx.Response(
        200, json={"next": "https://elsewhere.test/rates/", "results": []}
    ))
    outside = respx.get("https://elsewhere.test/rates/").mock(
        return_value=httpx.Response(200, json={"next": None, "results": []})
    )
    with pytest.raises(OctopusApiError, match="outside configured API"):
        await fetch_standard_unit_rates(_rates_settings(), period_from=datetime.now(UTC))
    assert not outside.called


@respx.mock
async def test_rates_error_on_non_json_body() -> None:
    """A 200 response with a non-JSON body (proxy/error page) degrades, never crashes."""
    respx.get(url__regex=r".*").mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    with pytest.raises(OctopusApiError, match="request failed"):
        await fetch_standard_unit_rates(
            _rates_settings(), period_from=datetime(2026, 6, 1, tzinfo=UTC)
        )


# --- planned dispatches (P8.5, #39) ---


def _dispatch_settings(**kw: str) -> Settings:
    base = dict(
        octopus_api_url=API, octopus_api_key="sk_test", octopus_account_number="A-1234ABCD"
    )
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


@respx.mock
async def test_dispatches_authenticates_then_queries() -> None:
    graphql = f"{API}/graphql/"
    route = respx.post(graphql).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-abc"}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "plannedDispatches": [
                            {"startDt": "2026-06-01T13:00:00Z", "endDt": "2026-06-01T13:30:00Z",
                             "delta": -2.0, "meta": {"source": "smart-charge"}}
                        ]
                    }
                },
            ),
        ]
    )
    slots = await fetch_planned_dispatches(_dispatch_settings())
    assert len(slots) == 1
    assert slots[0].charge_in_kwh == -2.0
    assert slots[0].source == "smart-charge"
    assert route.call_count == 2

    # The API key is sent once, only in the token-exchange variables — never
    # in the dispatches query, and never in the Authorization header (that
    # carries the short-lived JWT, not the raw key).
    token_call, dispatches_call = route.calls
    assert token_call.request.content.count(b"sk_test") == 1
    assert b"sk_test" not in dispatches_call.request.content
    assert dispatches_call.request.headers["authorization"] == "JWT jwt-abc"


@respx.mock
async def test_dispatches_auth_failure_raises() -> None:
    graphql = f"{API}/graphql/"
    respx.post(graphql).mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "invalid API key"}]})
    )
    with pytest.raises(OctopusApiError, match="auth rejected"):
        await fetch_planned_dispatches(_dispatch_settings())


@respx.mock
async def test_dispatches_error_on_non_json_auth_body() -> None:
    """A non-JSON auth response degrades to OctopusApiError, never crashes."""
    graphql = f"{API}/graphql/"
    respx.post(graphql).mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(OctopusApiError, match="auth request failed"):
        await fetch_planned_dispatches(_dispatch_settings())


@respx.mock
async def test_dispatches_error_on_non_json_query_body() -> None:
    """A non-JSON dispatches-query response degrades to OctopusApiError, never crashes."""
    graphql = f"{API}/graphql/"
    respx.post(graphql).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-abc"}}}),
            httpx.Response(200, content=b"not json"),
        ]
    )
    with pytest.raises(OctopusApiError, match="dispatches request failed"):
        await fetch_planned_dispatches(_dispatch_settings())


@respx.mock
async def test_dispatches_query_rejected_raises() -> None:
    graphql = f"{API}/graphql/"
    respx.post(graphql).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-abc"}}}),
            httpx.Response(200, json={"errors": [{"message": "bad account number"}]}),
        ]
    )
    with pytest.raises(OctopusApiError, match="dispatches request rejected"):
        await fetch_planned_dispatches(_dispatch_settings())


@respx.mock
async def test_dispatches_tolerates_malformed_entries() -> None:
    graphql = f"{API}/graphql/"
    respx.post(graphql).mock(
        side_effect=[
            httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": "jwt-abc"}}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "plannedDispatches": [
                            "not-a-dict",
                            {"startDt": "bad", "endDt": "also-bad", "delta": -1.0},
                            {"startDt": "2026-06-01T13:00:00Z", "endDt": "2026-06-01T13:30:00Z"},
                        ]
                    }
                },
            ),
        ]
    )
    slots = await fetch_planned_dispatches(_dispatch_settings())
    # The third entry parses (no `delta` -> charge_in_kwh defaults to 0.0);
    # the first two are skipped.
    assert len(slots) == 1
    assert slots[0].charge_in_kwh == 0.0


async def test_dispatches_error_when_unconfigured() -> None:
    with pytest.raises(OctopusApiError, match="not configured"):
        await fetch_planned_dispatches(_dispatch_settings(octopus_account_number=""))


# --- shared Kraken token (#688) ---

GRAPHQL = f"{API}/graphql/"
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _jwt(expires: datetime, tag: str = "a") -> str:
    def part(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'exp': int(expires.timestamp())})}.sig-{tag}"


def _token(token: str) -> httpx.Response:
    return httpx.Response(200, json={"data": {"obtainKrakenToken": {"token": token}}})


def _no_dispatches() -> httpx.Response:
    return httpx.Response(200, json={"data": {"plannedDispatches": []}})


def _mints(route: respx.Route) -> int:
    return sum(b"obtainKrakenToken" in call.request.content for call in route.calls)


def _freeze(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    monkeypatch.setattr(octopus, "_kraken_now", lambda: now)


@respx.mock
async def test_two_graphql_reads_mint_one_token() -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _no_dispatches(),
        httpx.Response(200, json=agreement_payload()),
    ])
    await fetch_planned_dispatches(_dispatch_settings())
    await fetch_agreement_rates(_dispatch_settings())
    assert route.call_count == 3
    assert _mints(route) == 1
    assert route.calls[2].request.headers["Authorization"] == "JWT jwt-private"


@respx.mock
async def test_token_is_replaced_shortly_before_it_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _jwt(T0 + timedelta(minutes=60), "first")
    second = _jwt(T0 + timedelta(minutes=120), "second")
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token(first), _no_dispatches(), _no_dispatches(),
        _token(second), _no_dispatches(),
    ])
    _freeze(monkeypatch, T0)
    await fetch_planned_dispatches(_dispatch_settings())
    _freeze(monkeypatch, T0 + timedelta(minutes=54))
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 1
    _freeze(monkeypatch, T0 + timedelta(minutes=56))
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 2
    assert route.calls[4].request.headers["Authorization"] == f"JWT {second}"


@pytest.mark.parametrize("token", [
    "not-a-jwt", "a.b.c", "a.e30.c",
    "a." + base64.urlsafe_b64encode(b'{"exp": "soon"}').decode() + ".c",
    "a." + base64.urlsafe_b64encode(b'{"exp": 1e999}').decode() + ".c",
    "a." + base64.urlsafe_b64encode(b'{"exp": true}').decode() + ".c",
    "a." + base64.urlsafe_b64encode(b"[1]").decode() + ".c",
])
@respx.mock
async def test_token_with_unreadable_expiry_is_kept_only_briefly(
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token(token), _no_dispatches(), _no_dispatches(), _token(token), _no_dispatches(),
    ])
    _freeze(monkeypatch, T0)
    await fetch_planned_dispatches(_dispatch_settings())
    _freeze(monkeypatch, T0 + timedelta(minutes=4))
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 1
    _freeze(monkeypatch, T0 + timedelta(minutes=6))
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 2


@respx.mock
async def test_token_expiry_claim_beyond_an_hour_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token(_jwt(T0 + timedelta(days=365))), _no_dispatches(),
        _token(_jwt(T0 + timedelta(days=365))), _no_dispatches(),
    ])
    _freeze(monkeypatch, T0)
    await fetch_planned_dispatches(_dispatch_settings())
    _freeze(monkeypatch, T0 + timedelta(minutes=56))
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 2


def _jwt_exp(exp: object) -> str:
    return "a." + base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode() + ".c"


@pytest.mark.parametrize("exp", [
    -62135596800, 0, int(T0.timestamp()), int((T0 + timedelta(minutes=5)).timestamp()),
])
@respx.mock
async def test_token_already_due_for_refresh_serves_one_read_and_is_not_kept(
    monkeypatch: pytest.MonkeyPatch, exp: int,
) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token(_jwt_exp(exp)), _no_dispatches(), _token(_jwt_exp(exp)), _no_dispatches(),
    ])
    _freeze(monkeypatch, T0)
    assert await fetch_planned_dispatches(_dispatch_settings()) == ()
    assert not octopus._kraken_tokens
    assert await fetch_planned_dispatches(_dispatch_settings()) == ()
    assert _mints(route) == 2


@respx.mock
async def test_malformed_endpoint_degrades_without_echoing_it() -> None:
    settings = _dispatch_settings(octopus_api_url="http://octo.test:sk_test")
    for read in (fetch_planned_dispatches, fetch_agreement_rates):
        with pytest.raises(OctopusApiError) as error:
            await read(settings)
        assert _chain(error.value) == [error.value]
        assert "sk_test" not in str(error.value)


def _rejected(kind: str) -> httpx.Response:
    if kind == "http":
        return httpx.Response(401, text="sk_test jwt-private")
    code = "KT-CT-1124" if kind == "expired" else "KT-CT-1139"
    return httpx.Response(200, json={"errors": [{
        "message": "sk_test jwt-private", "extensions": {"errorCode": code},
    }]})


@pytest.mark.parametrize("kind", ["http", "expired", "failed"])
@respx.mock
async def test_rejected_token_is_refreshed_once_and_the_read_retried_once(kind: str) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _no_dispatches(),
        _rejected(kind), _token("jwt-second"), httpx.Response(200, json=agreement_payload()),
        _no_dispatches(),
    ])
    await fetch_planned_dispatches(_dispatch_settings())
    rates = await fetch_agreement_rates(_dispatch_settings())
    assert rates.offpeak == pytest.approx(0.066)
    assert route.call_count == 5
    assert _mints(route) == 2
    assert route.calls[4].request.headers["Authorization"] == "JWT jwt-second"
    # The replacement is the shared token from then on.
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 2
    assert route.calls[5].request.headers["Authorization"] == "JWT jwt-second"


@pytest.mark.parametrize("kind", ["http", "expired", "failed"])
@respx.mock
async def test_token_still_rejected_after_refresh_degrades_without_a_second_retry(
    kind: str,
) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _no_dispatches(),
        _rejected(kind), _token("jwt-second"), _rejected(kind),
    ])
    await fetch_planned_dispatches(_dispatch_settings())
    with pytest.raises(OctopusApiError):
        await fetch_planned_dispatches(_dispatch_settings())
    assert route.call_count == 5
    assert not octopus._kraken_tokens  # the refused replacement is not kept


@pytest.mark.parametrize("kind", ["http", "expired", "failed"])
@respx.mock
async def test_newly_minted_token_that_is_rejected_is_not_retried_or_kept(kind: str) -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _rejected(kind), _token("jwt-second"), _no_dispatches(),
    ])
    with pytest.raises(OctopusApiError):
        await fetch_planned_dispatches(_dispatch_settings())
    assert route.call_count == 2
    # The next read starts from a new token, never the refused one.
    assert await fetch_planned_dispatches(_dispatch_settings()) == ()
    assert route.calls[3].request.headers["Authorization"] == "JWT jwt-second"


@respx.mock
async def test_failed_refresh_degrades_and_the_next_read_mints_again() -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _no_dispatches(),
        _rejected("http"), httpx.Response(500),
        _token("jwt-second"), _no_dispatches(),
    ])
    await fetch_planned_dispatches(_dispatch_settings())
    with pytest.raises(OctopusApiError, match="auth request failed"):
        await fetch_planned_dispatches(_dispatch_settings())
    assert await fetch_planned_dispatches(_dispatch_settings()) == ()
    assert route.calls[5].request.headers["Authorization"] == "JWT jwt-second"


@respx.mock
async def test_changed_api_key_mints_a_new_token() -> None:
    route = respx.post(GRAPHQL).mock(side_effect=[
        _token("jwt-private"), _no_dispatches(), _token("jwt-other"), _no_dispatches(),
        _token("jwt-third"), _no_dispatches(),
    ])
    await fetch_planned_dispatches(_dispatch_settings())
    await fetch_planned_dispatches(_dispatch_settings(octopus_api_key="sk_other"))
    assert _mints(route) == 2
    assert b"sk_other" in route.calls[2].request.content
    assert route.calls[3].request.headers["Authorization"] == "JWT jwt-other"
    # Changing back does not revive the first key's token either.
    await fetch_planned_dispatches(_dispatch_settings())
    assert _mints(route) == 3


@respx.mock
async def test_changed_endpoint_mints_a_new_token() -> None:
    first = respx.post(GRAPHQL).mock(side_effect=[_token("jwt-private"), _no_dispatches()])
    other = respx.post("http://other.test/v1/graphql/").mock(
        side_effect=[_token("jwt-other"), _no_dispatches()]
    )
    await fetch_planned_dispatches(_dispatch_settings())
    await fetch_planned_dispatches(_dispatch_settings(octopus_api_url="http://other.test/v1"))
    assert _mints(first) == 1
    assert _mints(other) == 1
    assert other.calls[1].request.headers["Authorization"] == "JWT jwt-other"


def _chain(error: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [error]
    while todo:
        item = todo.pop()
        if item is not None and item not in seen:
            seen.append(item)
            todo += [item.__cause__, item.__context__]
    return seen


@pytest.mark.parametrize("failure", [
    "mint_http", "mint_errors", "mint_shape", "mint_transport", "mint_not_json",
    "query_http", "query_rejected", "query_transport", "query_not_json", "query_errors",
    "refresh_http",
])
@pytest.mark.parametrize(
    "read", [fetch_planned_dispatches, fetch_agreement_rates], ids=["dispatches", "rates"]
)
@respx.mock
async def test_key_and_token_never_reach_logs_or_errors(
    failure: str, read: Any, caplog: pytest.LogCaptureFixture
) -> None:
    echo = "sk_test jwt-private"
    transport = httpx.ConnectError(echo)
    responses: dict[str, list[Any]] = {
        "mint_http": [httpx.Response(500, text=echo)],
        "mint_errors": [httpx.Response(200, json={"errors": [{"message": echo}]})],
        "mint_shape": [httpx.Response(200, json={"data": {"obtainKrakenToken": [echo]}})],
        "mint_transport": [transport],
        "mint_not_json": [httpx.Response(200, text=echo)],
        "query_http": [_token("jwt-private"), httpx.Response(500, text=echo)],
        "query_rejected": [_token("jwt-private"), _rejected("http")],
        "query_transport": [_token("jwt-private"), transport],
        "query_not_json": [_token("jwt-private"), httpx.Response(200, text=echo)],
        "query_errors": [_token("jwt-private"), _rejected("expired")],
        "refresh_http": [
            _token("jwt-private"), _no_dispatches(), _rejected("http"),
            httpx.Response(500, text=echo),
        ],
    }
    respx.post(GRAPHQL).mock(side_effect=responses[failure])
    caplog.set_level(logging.DEBUG)
    if failure == "refresh_http":
        await fetch_planned_dispatches(_dispatch_settings())
    with pytest.raises(OctopusApiError) as error:
        await read(_dispatch_settings())
    chain = _chain(error.value)
    assert chain == [error.value]  # no failed request rides along as cause or context
    exposed = "".join(
        [caplog.text, repr(octopus._kraken_tokens)]
        + [text for item in chain for text in (str(item), repr(item), repr(item.args))]
        + traceback.format_exception(error.value)
    )
    assert "sk_test" not in exposed
    assert "jwt-private" not in exposed


@respx.mock
async def test_cached_token_and_key_stay_out_of_the_cache_repr() -> None:
    respx.post(GRAPHQL).mock(side_effect=[_token("jwt-private"), _no_dispatches()])
    await fetch_planned_dispatches(_dispatch_settings())
    assert octopus._kraken_tokens
    shown = repr(octopus._kraken_tokens) + str(octopus._kraken_tokens)
    assert "jwt-private" not in shown
    assert "sk_test" not in shown


@respx.mock
async def test_token_mint_is_never_redirected() -> None:
    elsewhere = respx.post("http://elsewhere.test/graphql/").mock(
        return_value=_token("jwt-private")
    )
    respx.post(GRAPHQL).mock(
        return_value=httpx.Response(307, headers={"Location": "http://elsewhere.test/graphql/"})
    )
    with pytest.raises(OctopusApiError):
        await fetch_planned_dispatches(_dispatch_settings())
    assert not elsewhere.called


@respx.mock
async def test_failed_mint_raises_only_the_readers_own_error_type() -> None:
    respx.post(GRAPHQL).mock(side_effect=httpx.ReadTimeout("sk_test"))
    for read in (fetch_planned_dispatches, fetch_agreement_rates):
        with pytest.raises(OctopusApiError):
            await read(_dispatch_settings())
