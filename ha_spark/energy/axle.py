"""Axle event parsing for the supervised flexibility-event prototype."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ha_spark.config import Settings
from ha_spark.energy.models import FlexibilityEvent
from ha_spark.ha.rest import HomeAssistantRest

_MAX_EVENT_AGE = timedelta(minutes=10)


class AxleApiError(RuntimeError):
    """Raised when an Axle event cannot be trusted as a schedule input."""


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AxleApiError(f"Axle event has no usable {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AxleApiError(f"Axle event has an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise AxleApiError(f"Axle event {field} must include a timezone")
    return parsed.astimezone(UTC)


def parse_axle_event(
    payload: Mapping[str, Any] | None,
    *,
    now: datetime,
    rate_gbp_kwh: float,
) -> FlexibilityEvent | None:
    """Parse one Axle event snapshot, rejecting ambiguous or stale data.

    An empty object means that Axle has no next event. Any partially populated
    object is malformed and fails closed. Import events are valid Axle data but
    are intentionally ignored by this export-only prototype.
    """
    if payload is None or not payload:
        return None
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not math.isfinite(rate_gbp_kwh) or rate_gbp_kwh < 0:
        raise AxleApiError("Axle event rate is not a finite non-negative number")
    if "start_time" not in payload:
        raise AxleApiError("Axle event is missing start_time")

    start = _timestamp(payload.get("start_time"), "start_time")
    end = _timestamp(payload.get("end_time"), "end_time")
    updated_at = _timestamp(payload.get("updated_at"), "updated_at")
    if end <= start:
        raise AxleApiError("Axle event end_time must be after start_time")
    age = now.astimezone(UTC) - updated_at
    if age < timedelta(0) or age > _MAX_EVENT_AGE:
        raise AxleApiError("Axle event is stale or dated in the future")

    direction = payload.get("import_export")
    if not isinstance(direction, str) or direction not in {"import", "export"}:
        raise AxleApiError("Axle event has an invalid import_export direction")
    if end <= now.astimezone(UTC):
        return None
    if direction == "import":
        return None
    return FlexibilityEvent(start, end, direction, updated_at, rate_gbp_kwh)


async def fetch_axle_event(
    settings: Settings, *, now: datetime | None = None
) -> FlexibilityEvent | None:
    """Fetch the documented per-user Axle Home Assistant event snapshot."""
    if not settings.axle_api_key:
        raise AxleApiError("Axle API key is not configured")
    observed_at = now or datetime.now(UTC)
    url = f"{settings.axle_api_url.rstrip('/')}/vpp/home-assistant/event"
    try:
        async with httpx.AsyncClient(
            timeout=settings.ha_timeout,
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.axle_api_key}",
            },
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AxleApiError("Axle event request failed") from exc
    if payload is not None and not isinstance(payload, dict):
        raise AxleApiError("Axle event response is not an object")
    return parse_axle_event(
        payload,
        now=observed_at,
        rate_gbp_kwh=settings.axle_event_rate_gbp_kwh,
    )


async def read_axle_event(
    settings: Settings,
    rest: HomeAssistantRest,
    *,
    now: datetime | None = None,
) -> FlexibilityEvent | None:
    """Read Axle directly, falling back to the configured HA mirror on failure."""
    if settings.axle_api_key:
        try:
            return await fetch_axle_event(settings, now=now)
        except AxleApiError:
            if not settings.axle_event_entity:
                raise
    if not settings.axle_event_entity:
        raise AxleApiError("Axle event source is not configured")
    try:
        state = await rest.get_state(settings.axle_event_entity)
    except Exception as exc:  # noqa: BLE001 - source failures degrade the schedule
        raise AxleApiError("Home Assistant Axle event read failed") from exc
    payload = dict(state.attributes)
    state_value = state.state.strip().lower()
    if state_value in {"unknown", "unavailable"}:
        raise AxleApiError("Home Assistant Axle event state is unavailable")
    if "start_time" not in payload and state_value:
        payload["start_time"] = state.state
    return parse_axle_event(
        payload or None,
        now=now or datetime.now(UTC),
        rate_gbp_kwh=settings.axle_event_rate_gbp_kwh,
    )
