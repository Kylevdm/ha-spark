"""Turn natural-language context statements into stored planner facts.

Shared by both router tiers (Phase 6D): the remote LLM extracts a fact as
strict JSON, the offline parser extracts the same shape with deterministic
date parsing, and both funnel through one validated :class:`ExtractedContext`
and one :func:`record_context` writer. This path only ever *writes reviewable
facts* to the context store — it never actuates hardware (a ROADMAP non-goal).
Every recorded fact is echoed back with its planner effect and an undo command.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator

from ha_spark.config import Settings
from ha_spark.energy.context import ContextStore
from ha_spark.logging import get_logger

log = get_logger(__name__)

# Sensible defaults when an LLM names a *_usage fact without an explicit factor.
_DEFAULT_USAGE_FACTOR: dict[str, float] = {"high_usage": 1.3, "low_usage": 0.6}


class ExtractedContext(BaseModel):
    """A validated context fact extracted from a user message."""

    kind: Literal["away", "guests", "high_usage", "low_usage"]
    start: date
    end: date
    note: str = ""
    factor: float | None = None

    @model_validator(mode="after")
    def _check_range(self) -> ExtractedContext:
        if self.end < self.start:
            raise ValueError("end date is before the start date")
        return self


def effect_factor(extracted: ExtractedContext, settings: Settings) -> float:
    """The load multiplier this fact will apply (matches ContextEntry.factor)."""
    if extracted.kind == "away":
        return settings.away_load_factor
    if extracted.kind == "guests":
        return settings.guests_load_factor
    if extracted.factor is not None:
        return extracted.factor
    return _DEFAULT_USAGE_FACTOR.get(extracted.kind, 1.0)


def _stored_factor(extracted: ExtractedContext) -> float | None:
    """The factor to persist: only *_usage facts store one (away/guests use config)."""
    if extracted.kind in _DEFAULT_USAGE_FACTOR:
        return extracted.factor if extracted.factor is not None else _DEFAULT_USAGE_FACTOR[
            extracted.kind
        ]
    return None


def _span(extracted: ExtractedContext) -> str:
    if extracted.start == extracted.end:
        return f"{extracted.start:%a %d %b %Y}"
    return f"{extracted.start:%a %d %b} – {extracted.end:%a %d %b %Y}"


async def record_context(
    settings: Settings, extracted: ExtractedContext, *, source: str
) -> str:
    """Write the fact to the store and return a confirmation echoing its effect."""
    async with ContextStore(settings.db_path) as store:
        entry_id = await store.add(
            extracted.kind,
            extracted.start,
            extracted.end,
            note=extracted.note,
            source=source,
            factor=_stored_factor(extracted),
        )
    pct = effect_factor(extracted, settings) * 100.0
    note = f" ({extracted.note})" if extracted.note else ""
    return (
        f"Noted — {extracted.kind} {_span(extracted)}{note}. "
        f"The planner will assume ~{pct:.0f}% of normal load on those days. "
        f"Undo with `ha-spark context remove {entry_id}`."
    )


# --- LLM extraction -------------------------------------------------------

_EXTRACTION_SYSTEM = """\
You extract household energy-context facts from the user's message.
Today is {today} ({weekday}). Reply with ONLY a JSON object — no prose, no code
fences — or the bare word null.

Emit JSON when the message states a period that will change home energy use:
  {{"kind": "<away|guests|high_usage|low_usage>", "start": "YYYY-MM-DD",
    "end": "YYYY-MM-DD", "note": "<short summary>"}}
  - away: the home will be empty (holiday, trip, out of town).
  - guests: extra people staying.
  - high_usage / low_usage: unusual consumption; you may add "factor": <number>.
Resolve relative dates against today. The end date is inclusive; for a single
day, set end equal to start.

If the message is a question, a greeting, or not about presence/usage, reply
with exactly: null"""


def build_extraction_messages(message: str, today: date) -> list[dict[str, str]]:
    """The chat messages for the extraction pass."""
    system = _EXTRACTION_SYSTEM.format(today=today.isoformat(), weekday=f"{today:%A}")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": message},
    ]


def parse_llm_extraction(raw: str) -> ExtractedContext | None:
    """Parse the model's reply into a fact; None for ``null`` or any bad output."""
    text = raw.strip()
    # Tolerate code fences and surrounding prose: take the first {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None  # no JSON object (e.g. the literal "null")
    try:
        data = json.loads(text[start : end + 1])
        return ExtractedContext.model_validate(data)
    except (ValueError, ValidationError) as exc:
        log.info("Discarding unparseable extraction %r (%s)", raw[:120], exc)
        return None
