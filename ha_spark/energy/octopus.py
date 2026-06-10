"""Octopus Energy half-hourly consumption: CSV export parsing + REST API client."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from ha_spark.config import Settings
from ha_spark.energy.models import ConsumptionInterval
from ha_spark.logging import get_logger

log = get_logger(__name__)


class OctopusApiError(RuntimeError):
    """Raised when the Octopus API is misconfigured or returns an error."""


def _to_utc(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _find_column(fieldnames: list[str], prefix: str) -> str | None:
    for name in fieldnames:
        if name.strip().lower().startswith(prefix):
            return name
    return None


def parse_octopus_csv(text: str) -> list[ConsumptionInterval]:
    """Parse an Octopus dashboard consumption export.

    Header names drift across export vintages (`` Consumption (kwh)`` vs
    ``Consumption (kWh)``, leading spaces, ordering), so columns are matched by
    normalized prefix. Unparsable rows are skipped (counted in a warning).
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    fieldnames = list(reader.fieldnames or [])
    kwh_col = _find_column(fieldnames, "consumption")
    start_col = _find_column(fieldnames, "start")
    end_col = _find_column(fieldnames, "end")
    if not (kwh_col and start_col and end_col):
        raise ValueError(
            f"Unrecognized Octopus CSV header: {fieldnames!r} "
            "(expected Consumption/Start/End columns)"
        )

    intervals: list[ConsumptionInterval] = []
    skipped = 0
    for row in reader:
        try:
            intervals.append(
                ConsumptionInterval(
                    start=_to_utc(str(row[start_col])),
                    end=_to_utc(str(row[end_col])),
                    kwh=float(str(row[kwh_col]).strip()),
                )
            )
        except (KeyError, TypeError, ValueError):
            skipped += 1
    if skipped:
        log.warning("Skipped %d unparsable CSV row(s)", skipped)
    return intervals


def _parse_api_results(results: Any) -> list[ConsumptionInterval]:
    intervals: list[ConsumptionInterval] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        try:
            intervals.append(
                ConsumptionInterval(
                    start=_to_utc(str(item["interval_start"])),
                    end=_to_utc(str(item["interval_end"])),
                    kwh=float(item["consumption"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return intervals


async def fetch_consumption(
    settings: Settings, *, period_from: datetime, period_to: datetime | None = None
) -> list[ConsumptionInterval]:
    """Pull half-hourly consumption from the Octopus REST API (paginated)."""
    if not (settings.octopus_api_key and settings.octopus_mpan and settings.octopus_meter_serial):
        raise OctopusApiError(
            "Octopus API not configured: set OCTOPUS_API_KEY, OCTOPUS_MPAN and "
            "OCTOPUS_METER_SERIAL"
        )

    url = (
        f"{settings.octopus_api_url.rstrip('/')}/electricity-meter-points/"
        f"{settings.octopus_mpan}/meters/{settings.octopus_meter_serial}/consumption/"
    )
    params: dict[str, str] = {
        "period_from": period_from.astimezone(UTC).isoformat(),
        "page_size": "1500",
        "order_by": "period",
    }
    if period_to is not None:
        params["period_to"] = period_to.astimezone(UTC).isoformat()

    intervals: list[ConsumptionInterval] = []
    auth = httpx.BasicAuth(settings.octopus_api_key, "")
    async with httpx.AsyncClient(auth=auth, timeout=settings.ha_timeout) as client:
        next_url: str | None = url
        next_params: dict[str, str] | None = params
        while next_url:
            try:
                response = await client.get(next_url, params=next_params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise OctopusApiError(f"Octopus API request failed: {exc}") from exc
            payload = response.json()
            intervals.extend(_parse_api_results(payload.get("results")))
            # `next` is an absolute URL carrying the cursor; params ride along once.
            next_url = payload.get("next")
            next_params = None
    return intervals
