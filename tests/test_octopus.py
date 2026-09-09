"""Tests for Octopus CSV parsing and the consumption API client."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.octopus import (
    OctopusApiError,
    fetch_consumption,
    fetch_planned_dispatches,
    fetch_standard_unit_rates,
    parse_octopus_csv,
)

API = "http://octo.test/v1"


def _settings(api_key: str = "sk_test") -> Settings:
    return Settings(
        octopus_api_url=API,
        octopus_api_key=api_key,
        octopus_mpan="2200012282082",
        octopus_meter_serial="22L4386358",
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
    url = f"{API}/electricity-meter-points/2200012282082/meters/22L4386358/consumption/"
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
