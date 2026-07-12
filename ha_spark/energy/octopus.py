"""Octopus Energy half-hourly consumption, standard-unit-rates, and Intelligent
dispatch windows: CSV export parsing + REST/GraphQL API clients.

The consumption/rates endpoints are the public REST v1 API (API-key Basic
Auth). Planned Intelligent dispatches are only available over the Kraken
GraphQL API, which needs a short-lived JWT exchanged for the API key first
(``obtainKrakenToken``) — never logged, and never echoed in any error message.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

import httpx

from ha_spark.config import Settings
from ha_spark.energy.models import ConsumptionInterval, DispatchSlot, PricePoint
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


def _parse_rate_results(results: Any) -> list[PricePoint]:
    points: list[PricePoint] = []
    for item in results or []:
        if not isinstance(item, dict):
            continue
        try:
            points.append(
                PricePoint(
                    start=_to_utc(str(item["valid_from"])),
                    end=_to_utc(str(item["valid_to"])),
                    price=float(item["value_inc_vat"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return points


async def fetch_standard_unit_rates(
    settings: Settings, *, period_from: datetime, period_to: datetime | None = None
) -> tuple[PricePoint, ...]:
    """Pull per-slot import prices from the Octopus standard-unit-rates REST endpoint."""
    if not (
        settings.octopus_api_key
        and settings.octopus_product_code
        and settings.octopus_tariff_code
    ):
        raise OctopusApiError(
            "Octopus Intelligent not configured: set OCTOPUS_API_KEY, "
            "OCTOPUS_PRODUCT_CODE and OCTOPUS_TARIFF_CODE"
        )
    url = (
        f"{settings.octopus_api_url.rstrip('/')}/products/{settings.octopus_product_code}"
        f"/electricity-tariffs/{settings.octopus_tariff_code}/standard-unit-rates/"
    )
    params: dict[str, str] = {"period_from": period_from.astimezone(UTC).isoformat()}
    if period_to is not None:
        params["period_to"] = period_to.astimezone(UTC).isoformat()

    points: list[PricePoint] = []
    auth = httpx.BasicAuth(settings.octopus_api_key, "")
    async with httpx.AsyncClient(auth=auth, timeout=settings.ha_timeout) as client:
        next_url: str | None = url
        next_params: dict[str, str] | None = params
        while next_url:
            try:
                response = await client.get(next_url, params=next_params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise OctopusApiError(f"Octopus rates request failed: {exc}") from exc
            points.extend(_parse_rate_results(payload.get("results")))
            next_url = payload.get("next")
            next_params = None
    return tuple(sorted(points, key=lambda p: p.start))


_KRAKEN_TOKEN_QUERY = """
mutation ObtainKrakenToken($apiKey: String!) {
  obtainKrakenToken(input: { APIKey: $apiKey }) {
    token
  }
}
"""

_PLANNED_DISPATCHES_QUERY = """
query PlannedDispatches($account: String!) {
  plannedDispatches(accountNumber: $account) {
    startDt
    endDt
    delta
    meta { source }
  }
}
"""


async def _obtain_kraken_token(client: httpx.AsyncClient, graphql_url: str, api_key: str) -> str:
    """Exchange the Octopus API key for a short-lived Kraken GraphQL JWT.

    The key is sent once as a GraphQL variable (never interpolated into the
    query text); the returned token is used only in the Authorization header
    of the caller's next request, never logged.
    """
    try:
        response = await client.post(
            graphql_url, json={"query": _KRAKEN_TOKEN_QUERY, "variables": {"apiKey": api_key}}
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OctopusApiError(f"Octopus GraphQL auth request failed: {exc}") from exc
    if payload.get("errors"):
        raise OctopusApiError(
            f"Octopus GraphQL auth rejected: {len(payload['errors'])} error(s)"
        )
    token = ((payload.get("data") or {}).get("obtainKrakenToken") or {}).get("token")
    if not token:
        raise OctopusApiError("Octopus GraphQL auth returned no token")
    return str(token)


async def fetch_planned_dispatches(settings: Settings) -> tuple[DispatchSlot, ...]:
    """Planned Octopus Intelligent dispatch windows via the Kraken GraphQL API."""
    if not (settings.octopus_api_key and settings.octopus_account_number):
        raise OctopusApiError(
            "Octopus Intelligent not configured: set OCTOPUS_API_KEY and "
            "OCTOPUS_ACCOUNT_NUMBER"
        )
    graphql_url = f"{settings.octopus_api_url.rstrip('/')}/graphql/"
    async with httpx.AsyncClient(timeout=settings.ha_timeout) as client:
        token = await _obtain_kraken_token(client, graphql_url, settings.octopus_api_key)
        try:
            response = await client.post(
                graphql_url,
                json={
                    "query": _PLANNED_DISPATCHES_QUERY,
                    "variables": {"account": settings.octopus_account_number},
                },
                headers={"Authorization": f"JWT {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OctopusApiError(f"Octopus dispatches request failed: {exc}") from exc
    if payload.get("errors"):
        raise OctopusApiError(
            f"Octopus dispatches request rejected: {len(payload['errors'])} error(s)"
        )
    raw = (payload.get("data") or {}).get("plannedDispatches") or []
    slots: list[DispatchSlot] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = _to_utc(str(item["startDt"]))
            end = _to_utc(str(item["endDt"]))
        except (KeyError, ValueError, TypeError):
            continue
        try:
            charge_in_kwh = float(item["delta"])
        except (KeyError, TypeError, ValueError):
            charge_in_kwh = 0.0
        source = str((item.get("meta") or {}).get("source") or "")
        slots.append(DispatchSlot(start, end, charge_in_kwh, source))
    return tuple(slots)
