"""Tests for the Open-Meteo hourly temperature client."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from ha_spark.energy.weather import OPEN_METEO_URL, hourly_temps


@respx.mock
async def test_hourly_temps_parses_unixtime_and_drops_nulls() -> None:
    t0 = int(datetime(2026, 6, 12, 0, 0, tzinfo=UTC).timestamp())
    route = respx.get(OPEN_METEO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "hourly": {
                    "time": [t0, t0 + 3600, t0 + 7200],
                    "temperature_2m": [10.5, None, 12.0],
                }
            },
        )
    )
    temps = await hourly_temps(51.5, -0.1, past_days=2)
    assert route.called
    assert temps == {
        datetime(2026, 6, 12, 0, 0, tzinfo=UTC): 10.5,
        datetime(2026, 6, 12, 2, 0, tzinfo=UTC): 12.0,
    }


@respx.mock
async def test_hourly_temps_clamps_past_days() -> None:
    route = respx.get(OPEN_METEO_URL).mock(
        return_value=httpx.Response(200, json={"hourly": {}})
    )
    await hourly_temps(51.5, -0.1, past_days=500)
    assert route.calls[0].request.url.params["past_days"] == "92"


@respx.mock
async def test_hourly_temps_raises_on_http_error() -> None:
    respx.get(OPEN_METEO_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPError):
        await hourly_temps(51.5, -0.1, past_days=7)
