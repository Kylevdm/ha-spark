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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias

from ha_spark.config import Settings
from ha_spark.energy.forecast import load_timezone
from ha_spark.logging import get_logger

log = get_logger(__name__)

SolarSourceKind: TypeAlias = Literal["native", "entities"]

# Placeholder until #519 ticket 05 defines the parsed native forecast type.
NativeForecast: TypeAlias = object

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
        data = json.loads(path.read_text(encoding="utf-8"))
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
        log.warning("Saving solar source kind failed", exc_info=True)
        tmp_path.unlink(missing_ok=True)


def solar_source_from_store(settings: Settings) -> SolarSource:
    """Context for callers without daemon state: the persisted kind, no snapshot."""
    return SolarSource(
        kind=load_solar_source_kind(settings),
        forecast=None,
        received_at=None,
        now=datetime.now(load_timezone(settings.timezone)),
    )
