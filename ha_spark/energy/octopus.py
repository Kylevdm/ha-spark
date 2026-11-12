"""Octopus Energy consumption, account tariff discovery, rates, and dispatches.

The consumption/account/rates endpoints are the public REST v1 API (API-key Basic
Auth). Account agreement rates and planned Intelligent dispatches use the Kraken
GraphQL API, which needs a short-lived JWT exchanged for the API key first
(``obtainKrakenToken``) — never logged, and never echoed in any error message.
Weekend Happy Hour bookings are read from the undocumented Octopus backend
GraphQL endpoint (``octopus_backend_api_url``, ADR-0004) with the same shared
token, read-only, degrading to a failed reading on any error.

Every GraphQL read goes through ``kraken_graphql``, which shares one in-memory
token per endpoint (#688): minted on first use, replaced shortly before its
expiry, and replaced once more (with one retry of the call) when Kraken rejects
it. The token is never persisted.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import httpx
from pydantic import AwareDatetime, BaseModel, Field, StrictBool, StrictStr, ValidationError

from ha_spark.config import Settings
from ha_spark.energy.models import ConsumptionInterval, DispatchSlot, LiveRates, PricePoint
from ha_spark.logging import get_logger

log = get_logger(__name__)


class OctopusApiError(RuntimeError):
    """Raised when the Octopus API is misconfigured or returns an error."""


class _Agreement(BaseModel):
    tariff_code: StrictStr
    valid_from: datetime
    valid_to: datetime | None


class _MeterPoint(BaseModel):
    mpan: StrictStr
    is_export: StrictBool
    agreements: list[_Agreement]


class _Property(BaseModel):
    electricity_meter_points: list[_MeterPoint]


class _Account(BaseModel):
    properties: list[_Property]


async def discover_import_tariff(settings: Settings) -> tuple[str, str]:
    """Read the current import tariff from the configured Octopus account API."""
    if not (settings.octopus_api_key and settings.octopus_account_number):
        raise OctopusApiError("Octopus Intelligent not configured: set API key and account number")
    url = f"{settings.octopus_api_url.rstrip('/')}/accounts/{settings.octopus_account_number}/"
    try:
        async with httpx.AsyncClient(
            auth=httpx.BasicAuth(settings.octopus_api_key, ""), timeout=settings.ha_timeout
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            account = _Account.model_validate(response.json())
    except (httpx.HTTPError, ValueError, ValidationError):
        raise OctopusApiError(
            "Octopus account discovery failed: HTTP or payload validation error"
        ) from None

    imports = [
        point for prop in account.properties for point in prop.electricity_meter_points
        if not point.is_export
    ]
    if len(imports) > 1 and not settings.octopus_mpan:
        raise OctopusApiError("Octopus account has multiple import meter points; set OCTOPUS_MPAN")
    if settings.octopus_mpan:
        imports = [point for point in imports if point.mpan == settings.octopus_mpan]
        if not imports:
            raise OctopusApiError("Configured OCTOPUS_MPAN matches no import meter point")
    if len(imports) != 1:
        raise OctopusApiError(f"Octopus account has {len(imports)} eligible import meter points")
    now = datetime.now(UTC)
    agreements = [
        item for item in imports[0].agreements
        if item.valid_from.tzinfo is not None
        and (item.valid_to is None or item.valid_to.tzinfo is not None)
        and item.valid_from <= now
        and (item.valid_to is None or now < item.valid_to)
    ]
    if len(agreements) != 1:
        raise OctopusApiError(
            f"Octopus import meter point has {len(agreements)} current agreements"
        )
    tariff_code = agreements[0].tariff_code
    match = re.fullmatch(r"E-[1-9][0-9]*R-([A-Z0-9]+(?:-[A-Z0-9]+)*)-([A-Z])", tariff_code)
    if match is None:
        raise OctopusApiError("Octopus current tariff code has an invalid shape")
    return match.group(1), tariff_code


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
    if not settings.octopus_api_key:
        raise OctopusApiError("Octopus Intelligent not configured: set OCTOPUS_API_KEY")
    if settings.octopus_product_code and settings.octopus_tariff_code:
        product_code, tariff_code = settings.octopus_product_code, settings.octopus_tariff_code
    else:
        product_code, tariff_code = await discover_import_tariff(settings)
    url = (
        f"{settings.octopus_api_url.rstrip('/')}/products/{product_code}"
        f"/electricity-tariffs/{tariff_code}/standard-unit-rates/"
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
            except (httpx.HTTPError, ValueError):
                raise OctopusApiError(
                    "Octopus rates request failed: HTTP or payload error"
                ) from None
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise OctopusApiError("Octopus rates returned an invalid payload")
            points.extend(_parse_rate_results(payload.get("results")))
            next_url = payload.get("next")
            if next_url is not None and (
                not isinstance(next_url, str)
                or not next_url.startswith(f"{settings.octopus_api_url.rstrip('/')}/")
            ):
                raise OctopusApiError("Octopus rates pagination points outside configured API")
            next_params = None
    return tuple(sorted(points, key=lambda p: p.start))


_KRAKEN_TOKEN_QUERY = """
mutation ObtainKrakenToken($apiKey: String!) {
  obtainKrakenToken(input: { APIKey: $apiKey }) {
    token
  }
}
"""

_AGREEMENT_RATES_QUERY = """
query AgreementRates($account: String!) {
  account(accountNumber: $account) {
    electricityAgreements(active: true) {
      validFrom validTo
      meterPoint { direction }
      tariff { __typename
        ... on HalfHourlyTariff { unitRates { validFrom validTo value } }
        ... on DayNightTariff { dayRate nightRate }
        ... on StandardTariff { unitRate }
        ... on ThreeRateTariff { dayRate nightRate offPeakRate }
      }
    }
  }
}
"""

_Pence = Annotated[float, Field(strict=True, ge=0, le=200, allow_inf_nan=False)]


class _UnitRate(BaseModel):
    validFrom: AwareDatetime
    validTo: AwareDatetime | None
    value: _Pence


class _HalfHourlyTariff(BaseModel):
    kind: Literal["HalfHourlyTariff"] = Field(alias="__typename")
    unitRates: list[_UnitRate]


class _DayNightTariff(BaseModel):
    kind: Literal["DayNightTariff"] = Field(alias="__typename")
    dayRate: _Pence
    nightRate: _Pence


class _StandardTariff(BaseModel):
    kind: Literal["StandardTariff"] = Field(alias="__typename")
    unitRate: _Pence


class _ThreeRateTariff(BaseModel):
    kind: Literal["ThreeRateTariff"] = Field(alias="__typename")
    dayRate: _Pence
    nightRate: _Pence
    offPeakRate: _Pence


class _AgreementMeterPoint(BaseModel):
    direction: Literal["IMPORT", "EXPORT"]


class _LiveAgreement(BaseModel):
    validFrom: AwareDatetime
    validTo: AwareDatetime | None
    meterPoint: _AgreementMeterPoint
    tariff: Annotated[
        _HalfHourlyTariff | _DayNightTariff | _StandardTariff | _ThreeRateTariff,
        Field(discriminator="kind"),
    ]


class _AgreementAccount(BaseModel):
    electricityAgreements: list[_LiveAgreement]


class _AgreementData(BaseModel):
    account: _AgreementAccount


class _AgreementResponse(BaseModel):
    data: _AgreementData
    errors: list[Any] | None = None


async def fetch_agreement_rates(settings: Settings) -> LiveRates:
    """Read current import agreement rates, never inferring cheap-window timing."""
    if not (settings.octopus_api_key and settings.octopus_account_number):
        raise OctopusApiError("Octopus agreement rates require API key and account number")
    graphql_url = f"{settings.octopus_api_url.rstrip('/')}/graphql/"
    payload: _AgreementResponse | None = None
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout) as client:
            payload = _AgreementResponse.model_validate(await kraken_graphql(
                client, graphql_url, settings.octopus_api_key,
                _AGREEMENT_RATES_QUERY, {"account": settings.octopus_account_number},
            ))
    except (httpx.HTTPError, ValueError, OctopusApiError, TypeError, AttributeError):
        pass
    if payload is None:
        # Raised outside the handler: the failed request carries the token.
        raise OctopusApiError("Octopus agreement rates HTTP, auth or payload error")
    if payload.errors:
        raise OctopusApiError("Octopus agreement rates GraphQL errors")
    now = datetime.now(UTC)
    agreements = [
        agreement for agreement in payload.data.account.electricityAgreements
        if agreement.meterPoint.direction == "IMPORT"
        and agreement.validFrom <= now
        and (agreement.validTo is None or now < agreement.validTo)
    ]
    if len(agreements) != 1:
        raise OctopusApiError("Octopus agreement rates need exactly one active import agreement")
    tariff = agreements[0].tariff
    if isinstance(tariff, _HalfHourlyTariff):
        values = {
            rate.value for rate in tariff.unitRates
            if rate.validFrom <= now and (rate.validTo is None or now < rate.validTo)
        }
    elif isinstance(tariff, _StandardTariff):
        values = {tariff.unitRate}
    elif isinstance(tariff, _ThreeRateTariff):
        values = {tariff.dayRate, tariff.nightRate, tariff.offPeakRate}
    else:
        values = {tariff.dayRate, tariff.nightRate}
    if len(values) not in (1, 2):
        raise OctopusApiError("Octopus agreement rates need one or two distinct current values")
    return LiveRates(offpeak=min(values) / 100, peak=max(values) / 100)


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


# Kraken documents a one-hour token. A longer `exp` claim is not believed, and a
# token whose expiry cannot be read is kept only briefly.
_TOKEN_MAX_LIFETIME = timedelta(minutes=60)
_TOKEN_UNREADABLE_LIFETIME = timedelta(minutes=5)
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
# Kraken reports a rejected token as HTTP 401 or, more often, as HTTP 200 with
# one of these error codes (expired JWT, authentication failed).
_AUTH_ERROR_CODES = frozenset({"KT-CT-1124", "KT-CT-1139"})


@dataclass(frozen=True)
class _CachedToken:
    """One endpoint's token. Secret fields stay out of the repr."""

    key_digest: bytes = field(repr=False)
    token: str = field(repr=False)
    refresh_at: datetime


# Keyed by GraphQL endpoint; the entry remembers which API key minted it, so a
# changed key or endpoint never reuses another key's token. Memory only.
_kraken_tokens: dict[str, _CachedToken] = {}


def forget_kraken_tokens() -> None:
    """Drop every cached Kraken token (the next read mints a new one)."""
    _kraken_tokens.clear()


def _kraken_now() -> datetime:
    return datetime.now(UTC)


def _key_digest(api_key: str) -> bytes:
    return hashlib.sha256(api_key.encode()).digest()


def _token_expiry(token: str) -> datetime | None:
    """The token's own `exp` claim, read without trusting anything else in it."""
    try:
        _header, body, _signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        exp = claims["exp"]
        if isinstance(exp, bool) or not isinstance(exp, int | float) or not math.isfinite(exp):
            return None
        return datetime.fromtimestamp(exp, UTC)
    except (ValueError, KeyError, TypeError, OverflowError, OSError, RecursionError):
        return None


async def _obtain_kraken_token(client: httpx.AsyncClient, graphql_url: str, api_key: str) -> str:
    """Exchange the Octopus API key for a short-lived Kraken GraphQL JWT.

    The key is sent once as a GraphQL variable (never interpolated into the
    query text). Failures carry fixed text only and are raised outside the
    handler, so the failed request (whose body holds the key) is not kept on
    the error as cause or context.
    """
    payload: Any = None
    failed = False
    try:
        response = await client.post(
            graphql_url, json={"query": _KRAKEN_TOKEN_QUERY, "variables": {"apiKey": api_key}}
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        failed = True
    if failed:
        raise OctopusApiError("Octopus GraphQL auth request failed: HTTP or payload error")
    if not isinstance(payload, dict):
        raise OctopusApiError("Octopus GraphQL auth returned an invalid payload")
    if payload.get("errors"):
        raise OctopusApiError("Octopus GraphQL auth rejected")
    data = payload.get("data")
    obtained = data.get("obtainKrakenToken") if isinstance(data, dict) else None
    token = obtained.get("token") if isinstance(obtained, dict) else None
    if not isinstance(token, str) or not token:
        raise OctopusApiError("Octopus GraphQL auth returned no token")
    return token


async def _kraken_token(
    client: httpx.AsyncClient, graphql_url: str, api_key: str, *, rejected: str | None = None
) -> tuple[str, bool]:
    """The shared token for this endpoint and key, and whether it was just minted.

    `rejected` is a token Kraken has just refused: it is never handed out again.
    """
    digest = _key_digest(api_key)
    cached = _kraken_tokens.get(graphql_url)
    now = _kraken_now()
    if (
        cached is not None
        and cached.key_digest == digest
        and cached.token != rejected
        and now < cached.refresh_at
    ):
        return cached.token, False
    _kraken_tokens.pop(graphql_url, None)
    token = await _obtain_kraken_token(client, graphql_url, api_key)
    expiry = _token_expiry(token)
    if expiry is None:
        keep_for = _TOKEN_UNREADABLE_LIFETIME
    else:
        keep_for = min(expiry - now, _TOKEN_MAX_LIFETIME) - _TOKEN_REFRESH_MARGIN
    # A token already due for refresh serves this one call and is not kept.
    if keep_for > timedelta(0):
        _kraken_tokens[graphql_url] = _CachedToken(digest, token, now + keep_for)
    return token, True


def _auth_rejected(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    try:
        payload = response.json()
    except ValueError:
        return False
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not isinstance(errors, list):
        return False
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("errorCode") if isinstance(extensions, dict) else None
        if isinstance(code, str) and code in _AUTH_ERROR_CODES:
            return True
    return False


async def kraken_graphql(
    client: httpx.AsyncClient,
    graphql_url: str,
    api_key: str,
    query: str,
    variables: dict[str, Any],
) -> Any:
    """POST one read-only GraphQL query with the shared Kraken token; return its JSON.

    `graphql_url` must be built from configured settings. If Kraken rejects a
    token this call did not mint itself, the token is replaced once and the
    query retried once.

    Raises `OctopusApiError` (fixed text) when no token can be obtained, and
    lets `httpx.HTTPError` / `ValueError` from the query itself through, so each
    reader words its own failure. A reader must catch all three and report a
    failed read with fixed text and `from None`: the failed request carries the
    token in its headers.
    """
    token, minted = await _kraken_token(client, graphql_url, api_key)
    body = {"query": query, "variables": variables}
    response = await client.post(graphql_url, json=body, headers={"Authorization": f"JWT {token}"})
    if _auth_rejected(response) and not minted:
        token, _minted = await _kraken_token(client, graphql_url, api_key, rejected=token)
        response = await client.post(
            graphql_url, json=body, headers={"Authorization": f"JWT {token}"}
        )
    if _auth_rejected(response):
        # Still refused: no later read is handed this token.
        cached = _kraken_tokens.get(graphql_url)
        if cached is not None and cached.token == token:
            del _kraken_tokens[graphql_url]
    response.raise_for_status()
    return response.json()


async def fetch_planned_dispatches(settings: Settings) -> tuple[DispatchSlot, ...]:
    """Planned Octopus Intelligent dispatch windows via the Kraken GraphQL API."""
    if not (settings.octopus_api_key and settings.octopus_account_number):
        raise OctopusApiError(
            "Octopus Intelligent not configured: set OCTOPUS_API_KEY and "
            "OCTOPUS_ACCOUNT_NUMBER"
        )
    graphql_url = f"{settings.octopus_api_url.rstrip('/')}/graphql/"
    failed = False
    async with httpx.AsyncClient(timeout=settings.ha_timeout) as client:
        try:
            payload = await kraken_graphql(
                client, graphql_url, settings.octopus_api_key,
                _PLANNED_DISPATCHES_QUERY, {"account": settings.octopus_account_number},
            )
        except (httpx.HTTPError, ValueError):
            failed = True
    if failed or not isinstance(payload, dict):
        # Raised outside the handler: the failed request carries the token.
        raise OctopusApiError("Octopus dispatches request failed: HTTP or payload error")
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


_HAPPY_HOURS_QUERY = """
query HappyHours($account: String!) {
  savingSessions { account(accountNumber: $account) {
    joinedEvents { eventId eventType startAt endAt } } }
}
"""


class _SavingSessionEvent(BaseModel):
    """One ``joinedEvents`` entry; only ``eventType`` is trusted by the model.

    The other fields are checked by hand: a bad ``eventId`` or a missing field is
    schema drift (the read fails), while unusable times drop only that booking.
    """

    eventType: StrictStr
    eventId: Any = None
    startAt: Any = None
    endAt: Any = None


class _SavingSessionAccount(BaseModel):
    joinedEvents: list[_SavingSessionEvent]


class _SavingSessions(BaseModel):
    account: _SavingSessionAccount


class _SavingSessionsData(BaseModel):
    savingSessions: _SavingSessions


class _HappyHourResponse(BaseModel):
    data: _SavingSessionsData
    errors: list[Any] | None = None


@dataclass(frozen=True)
class HappyHourBooking:
    """One booked Weekend Happy Hour (Octopus's numeric ``eventId`` as text)."""

    event_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class HappyHourReading:
    """One Happy Hour read: ``bookings`` is ``None`` when the read failed.

    ``()`` means read with no booking; a non-empty tuple means booked. The
    planner treats a failed read as no session, never as a booking.
    """

    read_at: datetime
    bookings: tuple[HappyHourBooking, ...] | None


def happy_hour_source(settings: Settings) -> Literal["disabled", "not_configured", "active"]:
    """Whether the Happy Hour source may read; never a ``tariff_provider`` question."""
    if not settings.happy_hour_enabled:
        return "disabled"
    if not (
        settings.octopus_api_key
        and settings.octopus_account_number
        and settings.octopus_backend_api_url.strip()
    ):
        return "not_configured"
    return "active"


def _happy_hour_event_id(raw: Any) -> str | None:
    """The booking's ``eventId`` as text, or ``None`` when missing or wrong-typed."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw)
    # Text is accepted only as digits: an id is never a place to echo anything.
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 20:
        return raw
    return None


_CALENDAR_START = datetime.min.replace(tzinfo=UTC) + timedelta(days=2)
_CALENDAR_END = datetime.max.replace(tzinfo=UTC) - timedelta(days=2)


def _booking_instant(raw: object) -> datetime:
    """Parse a booking time; it must carry its own UTC offset, never a guessed one."""
    if not isinstance(raw, str):
        raise ValueError("booking time is not a string")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("booking time carries no UTC offset")
    try:
        instant = parsed.astimezone(UTC)
    except OverflowError:
        raise ValueError("booking time is out of range") from None
    # Within two days of either end of the calendar it cannot be shown on every
    # household clock.
    if not _CALENDAR_START <= instant <= _CALENDAR_END:
        raise ValueError("booking time is out of range")
    return instant


async def fetch_happy_hours(
    settings: Settings, *, now: datetime | None = None
) -> HappyHourReading:
    """Read booked Weekend Happy Hours from the backend endpoint (ADR-0004).

    Never raises for anything Octopus sends or fails to send: any HTTP, transport,
    auth or payload failure is a failed reading, which the planner treats as no
    session. A booking whose times cannot be read as UTC-offset instants is
    dropped, never zone-guessed. Offered slots are not selected and can never
    become bookings. `now` is the read time (an aware instant; a naive one is
    replaced by the clock).
    """
    read_at = now if now is not None and now.tzinfo is not None else datetime.now(UTC)
    if happy_hour_source(settings) != "active":
        # Disabled or unconfigured: no request, and no booking implied.
        return HappyHourReading(read_at, None)
    graphql_url = f"{settings.octopus_backend_api_url.rstrip('/')}/graphql/"
    reply: _HappyHourResponse | None = None
    try:
        async with httpx.AsyncClient(timeout=settings.ha_timeout) as client:
            payload = await kraken_graphql(
                client, graphql_url, settings.octopus_api_key,
                _HAPPY_HOURS_QUERY, {"account": settings.octopus_account_number},
            )
        reply = _HappyHourResponse.model_validate(payload)
    except (httpx.HTTPError, ValueError, OctopusApiError, RecursionError):
        pass  # RecursionError: a reply nested too deeply to decode
    if reply is None or reply.errors:
        # Fixed text, outside the handler: the failed request carries the token.
        log.warning("Happy Hour read failed; no Happy Hour is planned")
        return HappyHourReading(read_at, None)
    bookings: dict[str, HappyHourBooking] = {}
    for event in reply.data.savingSessions.account.joinedEvents:
        if event.eventType != "WEEKEND_HAPPY_HOUR":
            continue
        event_id = _happy_hour_event_id(event.eventId)
        if event_id is None or event.startAt is None or event.endAt is None:
            # A missing field or a wrong-typed id is schema drift, not a booking.
            log.warning("Happy Hour read failed; no Happy Hour is planned")
            return HappyHourReading(read_at, None)
        try:
            start = _booking_instant(event.startAt)
            end = _booking_instant(event.endAt)
        except ValueError:
            continue  # dropped: this hour's times cannot be read as instants
        if end <= start:
            continue
        if end > read_at and event_id not in bookings:
            bookings[event_id] = HappyHourBooking(event_id, start, end)
    ordered = tuple(sorted(bookings.values(), key=lambda booking: booking.start))
    return HappyHourReading(read_at, ordered)
