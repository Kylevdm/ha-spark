"""Where a plan's solar forecast comes from: legacy HA entities, or native Solcast.

``gather_inputs`` takes a :class:`SolarSource` as a required input so planning
always states its solar source explicitly instead of silently reading the
forecast entities (``solar_today_entity``/``solar_tomorrow_entity``), which any
token holder can forge over REST (#322). The daemon builds one context per plan
from its last accepted integration snapshot; callers without daemon state (the
CLI's direct plan, agent previews, the backtest) read the persisted kind from
the add-on's data dir, so a restart cannot fall back to the entities either.

The kind is persisted as one small JSON file beside the other data-dir files.
The daemon is its only writer; a fresh install — and any missing, unreadable or
corrupt file — reads as ``entities``, the legacy path. The file holds the kind
and nothing else: no site ids, no secrets.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.logging import get_logger

log = get_logger(__name__)

SolarSourceKind: TypeAlias = Literal["native", "entities"]

NATIVE_PREVIEW_NOTE = (
    "solar: native Solcast forecast is only available inside the daemon; planning with zero solar"
)


@dataclass(frozen=True)
class NativeForecast:
    """One complete, immutable last-good fetch, in UTC mean kW."""

    status: str
    fetched_at: datetime | None
    calls_used: int
    calls_limit: int
    slots: tuple[tuple[datetime, float, float, float], ...]


@dataclass(frozen=True)
class ParsedSolarForecast:
    """An invalid field still selects native; only explicit null selects entities."""

    kind: SolarSourceKind
    forecast: NativeForecast | None = None


class _ForecastField(BaseModel):
    model_config = ConfigDict(strict=True)

    status: Literal["ok", "stale", "auth_failed", "quota_exhausted", "no_data"]
    fetched_at: AwareDatetime | None
    calls_used: int = Field(ge=0, le=1000)
    calls_limit: int
    slots: list[list[object]] = Field(max_length=120)

    @field_validator("fetched_at", mode="before")
    @classmethod
    def timestamp(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return _utc_instant(value)


def _utc_instant(value: object) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)", value,
    ) is None:
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("invalid_timestamp")
    return parsed.astimezone(UTC)


def parse_native_forecast(value: object, *, received_at: datetime) -> ParsedSolarForecast:
    """Validate independently: never expose payloads or reject a heartbeat."""
    if value is None:
        return ParsedSolarForecast("entities")
    try:
        field = _ForecastField.model_validate(value)
        if type(field.calls_limit) is not int or field.calls_limit not in {10, 50}:
            raise ValueError("invalid_limit")
        if field.fetched_at is not None and field.fetched_at > received_at + timedelta(seconds=5):
            raise ValueError("future_fetch")
        slots: list[tuple[datetime, float, float, float]] = []
        for row in field.slots:
            if len(row) != 4:
                raise ValueError("invalid_slot")
            start = _utc_instant(row[0])
            if start.minute % 30 or start.second or start.microsecond:
                raise ValueError("unaligned_slot")
            if slots and start != slots[-1][0] + timedelta(minutes=30):
                raise ValueError("noncontiguous_slots")
            numbers: list[float] = []
            for raw in row[1:]:
                if type(raw) not in (int, float) or not isinstance(raw, (int, float)):
                    raise ValueError("invalid_power")
                number = float(raw)
                if not 0 <= number <= 100:
                    raise ValueError("invalid_power")
                numbers.append(number)
            p10, p50, p90 = numbers
            if not p10 <= p50 <= p90:
                raise ValueError("invalid_percentiles")
            slots.append((start, p10, p50, p90))
        if slots and field.fetched_at is None:
            raise ValueError("missing_fetch")
        # The integration sends from its current half-hour (contiguous, at most 120);
        # allow one half-hour of clock and transit skew at the start.
        current = received_at.astimezone(UTC).replace(
            minute=received_at.astimezone(UTC).minute // 30 * 30, second=0, microsecond=0,
        )
        if slots and not current - timedelta(minutes=30) <= slots[0][0] <= current:
            raise ValueError("outside_horizon")
        return ParsedSolarForecast("native", NativeForecast(
            field.status, field.fetched_at, field.calls_used, field.calls_limit, tuple(slots),
        ))
    except (ValidationError, ValueError, TypeError, OverflowError):
        return ParsedSolarForecast("native")


_KIND_FILE = "ha_spark_solar_source.json"


@dataclass(frozen=True)
class SolarSource:
    """Immutable, per-plan description of the solar source.

    ``kind`` is ``entities`` (the legacy forecast entities) or ``native``
    (the snapshot's Solcast forecast). ``forecast`` is the parsed native
    forecast, or ``None`` when absent — including every ``entities`` plan.
    ``received_at`` is the snapshot's received instant, or ``None`` without
    one. ``now`` is the plan's instant as the caller states it: a native
    source, once built, will judge forecast age at it, while the legacy
    entities path keeps its own clock read unchanged.
    """

    kind: SolarSourceKind
    forecast: NativeForecast | None
    received_at: datetime | None
    now: datetime


def _kind_path(settings: Settings) -> Path:
    return Path(settings.db_path).parent / _KIND_FILE


def load_solar_source_kind(settings: Settings) -> SolarSourceKind:
    """Load the persisted kind; missing, unreadable or corrupt reads as entities."""
    path = _kind_path(settings)
    if not path.is_file():
        return "entities"
    try:
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size > 1024:
                return "entities"
            data = json.loads(stream.read(1024))
    except (OSError, ValueError, TypeError, RecursionError):
        log.warning("Reading solar source kind failed; using entities", exc_info=True)
        return "entities"
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind == "native":
        return "native"
    if kind == "entities":
        return "entities"
    return "entities"


def save_solar_source_kind(settings: Settings, kind: SolarSourceKind) -> None:
    """Persist the kind (atomic via tmp-file + rename). Daemon-only writer."""
    path = _kind_path(settings)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps({"kind": kind}), encoding="utf-8")
        os.replace(tmp_path, path)  # stdlib atomic rename, as the V2L session store
    except OSError:
        log.warning("Saving solar source kind failed")
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def solar_source_from_store(settings: Settings) -> SolarSource:
    """Context for callers without daemon state: the persisted kind, no snapshot."""
    return SolarSource(
        kind=load_solar_source_kind(settings),
        forecast=None,
        received_at=None,
        now=datetime.now(load_timezone(settings.timezone)),
    )
