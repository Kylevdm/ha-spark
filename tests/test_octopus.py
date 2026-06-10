"""Tests for Octopus CSV parsing and the consumption API client."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.energy.octopus import OctopusApiError, fetch_consumption, parse_octopus_csv

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
