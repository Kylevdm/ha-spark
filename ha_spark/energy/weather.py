"""Open-Meteo hourly temperature client (free, keyless).

One forecast-API call covers the whole training window: ``past_days`` (up to
92 — more than ``profile_history_days``) of observed/reanalysis temperatures
plus ``forecast_days`` ahead, so no separate archive endpoint is needed.
Callers cache past hours into the signal ledger so the ML model can fall back
to sampled temperatures when Open-Meteo is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo's forecast API serves at most 92 past days.
MAX_PAST_DAYS = 92


async def hourly_temps(
    lat: float,
    lon: float,
    *,
    past_days: int,
    forecast_days: int = 2,
    timeout: float = 15.0,  # noqa: ASYNC109 - plain httpx timeout, no cancel scope
) -> dict[datetime, float]:
    """Hourly outdoor temperature (°C) keyed by tz-aware UTC hour start.

    Spans ``past_days`` back through ``forecast_days`` ahead. Null readings
    are dropped; raises ``httpx.HTTPError`` on transport/status failures.
    """
    params: dict[str, str | int | float] = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "past_days": max(0, min(past_days, MAX_PAST_DAYS)),
        "forecast_days": forecast_days,
        "timeformat": "unixtime",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(OPEN_METEO_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    return {
        datetime.fromtimestamp(float(t), UTC): float(v)
        for t, v in zip(times, temps, strict=False)
        if v is not None
    }
