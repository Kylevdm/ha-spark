"""Durable, best-effort notifications for the supervised Axle export path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

ExportTransition = Literal["accepted", "started", "cleanup", "aborted"]


@dataclass(frozen=True)
class ExportNotice:
    """One operator-facing export lifecycle notice."""

    event_id: str
    transition: ExportTransition
    title: str
    message: str


def parse_event_id(event_id: str) -> tuple[str, datetime, datetime] | None:
    """Decode the stable ``direction|start|end`` identity used by the export store."""
    parts = event_id.split("|", 2)
    if len(parts) != 3:
        return None
    try:
        start = datetime.fromisoformat(parts[1])
        end = datetime.fromisoformat(parts[2])
    except ValueError:
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    return parts[0], start, end


def _window(start: datetime, end: datetime) -> str:
    start_zone = start.tzname() or "local time"
    end_zone = end.tzname() or start_zone
    return f"{start:%Y-%m-%d %H:%M} {start_zone}-{end:%H:%M} {end_zone}"


def make_notice(
    transition: ExportTransition,
    event_id: str,
    start: datetime,
    end: datetime,
    *,
    planned_export_kw: float | None = None,
    dno_export_limit_kw: float | None = None,
    reason: str | None = None,
    safe_state: str | None = None,
) -> ExportNotice:
    """Build the four notices in the supervised-export operator contract."""
    window = _window(start, end)
    if transition == "accepted":
        planned = "unknown" if planned_export_kw is None else f"{planned_export_kw:g} kW"
        limit = "unknown" if dno_export_limit_kw is None else f"{dno_export_limit_kw:g} kW"
        title = "Axle export accepted"
        message = (
            f"Accepted Axle export for {window}. Planned export: {planned}; "
            f"DNO limit: {limit}. Required preparation: keep a person present, "
            "confirm proactive_mode is on and ha-spark has control authority, "
            "then return to simulate after verified cleanup."
        )
    elif transition == "started":
        title = "Axle export started"
        message = (
            f"Verified Axle export start for {window}: the Solis timed-discharge "
            "current and export window were written and read back successfully."
        )
    elif transition == "cleanup":
        title = "Axle export cleanup verified"
        message = (
            f"Verified Axle export cleanup for {window}: the resident timed export "
            "window was cleared and read back."
        )
    else:
        title = "Axle export aborted"
        message = f"Aborted Axle export for {window}: {reason or 'operation refused'}."
        if safe_state:
            message += f" Safe result: {safe_state}."
    return ExportNotice(event_id, transition, title, message)


class ExportNotificationStore:
    """Persist sent transition keys so polling and restarts do not repeat notices."""

    _MAX_EVENTS = 32

    def __init__(self, db_path: str) -> None:
        self._path = Path(f"{db_path}.solis-export-notifications.json")

    def _read(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or not isinstance(raw.get("events"), dict):
            return {}
        events: dict[str, list[str]] = {}
        for event_id, transitions in raw["events"].items():
            if isinstance(event_id, str) and isinstance(transitions, list):
                events[event_id] = [item for item in transitions if isinstance(item, str)]
        return events

    def _write(self, events: dict[str, list[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(json.dumps({"events": events}, sort_keys=True), encoding="utf-8")
        temporary.replace(self._path)

    async def sent(self, event_id: str, transition: ExportTransition) -> bool:
        return transition in self._read().get(event_id, [])

    async def mark_sent(self, event_id: str, transition: ExportTransition) -> None:
        events = self._read()
        sent = set(events.get(event_id, []))
        sent.add(transition)
        events[event_id] = sorted(sent)
        while len(events) > self._MAX_EVENTS:
            del events[next(iter(events))]
        self._write(events)


async def send_once(
    store: ExportNotificationStore,
    rest: HomeAssistantRest,
    service: str,
    notice: ExportNotice,
) -> bool:
    """Send one notice, marking it only after HA accepts the service call."""
    if not service or await store.sent(notice.event_id, notice.transition):
        return False
    try:
        await rest.call_service(
            "notify", service, {"title": notice.title, "message": notice.message}
        )
    except Exception:  # noqa: BLE001 - notification failure must not affect control
        log.warning("Axle export notification failed (%s)", notice.transition, exc_info=True)
        return False
    try:
        await store.mark_sent(notice.event_id, notice.transition)
    except Exception:  # noqa: BLE001 - control has succeeded; dedupe can retry next pass
        log.warning("Persisting Axle export notification state failed", exc_info=True)
    return True
