"""Durable state for the last verified Solis export event."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class ExportEventStore:
    """Async-shaped store with one durable accepted-event record.

    A JSON sidecar avoids contention with the forecast SQLite connection during
    ordinary scheduler ticks and follows the existing V2L/publish state files.
    """

    def __init__(self, db_path: str) -> None:
        self._path = Path(f"{db_path}.solis-export.json")

    @property
    def exists(self) -> bool:
        return self._path.exists()

    async def __aenter__(self) -> ExportEventStore:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def load(self) -> tuple[str, datetime] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return (
                str(data["event_id"]),
                datetime.fromisoformat(str(data["verified_end"])).astimezone(UTC),
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None

    async def save(self, event_id: str, verified_end: datetime) -> None:
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "event_id": event_id,
                    "verified_end": verified_end.astimezone(UTC).isoformat(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temporary.replace(self._path)

    async def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
