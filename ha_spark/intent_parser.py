"""Deterministic offline intent parser — the router's no-LLM fallback.

Handles energy/planner queries only: the message is matched against keyword
groups and answered from the same ``gather_inputs``/``compute_plan`` pipeline
the ``plan`` command uses. Anything unrecognised returns help text listing the
supported queries; this path must never raise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from ha_spark.config import Settings
from ha_spark.context_intent import ExtractedContext
from ha_spark.energy.context import ContextEntry, ContextStore
from ha_spark.energy.models import ChargePlan
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.report import format_plan
from ha_spark.energy.sources import gather_inputs
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.logging import get_logger

log = get_logger(__name__)

_HELP = """\
I couldn't match that to a known query (and the LLM is unavailable).
Offline, I can answer questions containing:
  plan / charge / tonight / overnight   tonight's full charge plan
  soc / battery / state of charge       current battery state of charge
  solar / forecast                      tomorrow's solar forecast
  strategy                              the configured charge strategy
  mode / proactive                      the PROACTIVE_MODE setting
  window                                the off-peak charge window"""


@dataclass(frozen=True)
class IntentResult:
    """The offline parser's answer: text plus whether a query was recognised."""

    text: str
    matched: bool


def _contains(message: str, *keywords: str) -> bool:
    return any(k in message for k in keywords)


def _soc_line(plan: ChargePlan) -> str:
    if not plan.soc_valid:
        return "Battery SoC sensor is unreadable right now."
    return f"Battery is at {plan.soc_now:.0f}% ({plan.usable_now_kwh:.2f} kWh usable)."


def _solar_line(plan: ChargePlan) -> str:
    line = f"Solar forecast for tomorrow: {plan.solar_kwh:.2f} kWh"
    if abs(plan.effective_solar_kwh - plan.solar_kwh) > 1e-9:
        line += f" ({plan.effective_solar_kwh:.2f} kWh after haircut)"
    return line + "."


async def parse_offline(
    message: str, settings: Settings, rest: HomeAssistantRest
) -> IntentResult:
    """Answer an energy query deterministically, without an LLM."""
    text = message.lower()

    if not _contains(
        text,
        "plan", "charge", "tonight", "overnight",
        "soc", "battery", "state of charge",
        "solar", "forecast",
        "strategy", "mode", "proactive", "window",
    ):
        return IntentResult(_HELP, matched=False)

    # Settings-only queries need no HA round-trip.
    if _contains(text, "strategy"):
        return IntentResult(f"Charge strategy: {settings.charge_strategy}.", matched=True)
    if _contains(text, "mode", "proactive"):
        return IntentResult(f"PROACTIVE_MODE: {settings.proactive_mode}.", matched=True)
    if _contains(text, "window"):
        return IntentResult(
            f"Charge window: {settings.charge_window_start} – {settings.charge_window_end}.",
            matched=True,
        )

    # Everything else needs the computed plan.
    try:
        inputs, cfg, load_source = await gather_inputs(settings, rest)
        plan = compute_plan(inputs, cfg)
    except Exception as exc:  # noqa: BLE001 - the fallback path must never crash
        log.warning("Offline parser could not compute the plan: %r", exc)
        return IntentResult(f"Could not compute the charge plan: {exc}", matched=True)

    if _contains(text, "soc", "battery", "state of charge"):
        return IntentResult(_soc_line(plan), matched=True)
    if _contains(text, "solar", "forecast"):
        return IntentResult(_solar_line(plan), matched=True)
    return IntentResult(format_plan(plan, load_source), matched=True)


# --- Phase 6D: offline context extraction + query (the no-LLM fallback) ---

_AWAY_WORDS = (
    "away", "holiday", "vacation", "on leave", "out of town", "abroad",
    "won't be home", "wont be home", "will not be home", "not be home",
    "not home", "trip",
)
_GUEST_WORDS = (
    "guest", "guests", "visitor", "visitors", "staying over", "family staying",
    "friends staying", "people over", "company over", "having people",
)

# Words that hint a message might set context; gates the (possibly LLM) pass.
_CONTEXT_HINTS = _AWAY_WORDS + _GUEST_WORDS + (
    "next week", "this week", "weekend", "tomorrow", "fortnight", "until", "back on",
)

_NUMBER_WORDS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_ISO = r"\d{4}-\d{2}-\d{2}"


def mentions_context(text: str) -> bool:
    """Cheap pre-filter: could this message be setting a context fact?"""
    return _contains(text, *_CONTEXT_HINTS) or bool(re.search(_ISO, text))


def is_context_query(text: str) -> bool:
    """True for questions about stored context (answered from the store)."""
    has_question = "?" in text or text.lstrip().startswith(
        ("what", "which", "when", "do ", "does ", "is ", "are ", "am i", "list", "show", "tell me")
    )
    topic = _contains(
        text, "context", "holiday", "away", "guest", "upcoming", "planned",
        "scheduled", "absence",
    )
    return has_question and topic


def _kind_from(text: str) -> str | None:
    if _contains(text, *_GUEST_WORDS):
        return "guests"
    if _contains(text, *_AWAY_WORDS):
        return "away"
    return None


def _count(token: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _date_range(text: str, today: date) -> tuple[date, date] | None:
    """Resolve a date range from the message; None when nothing parseable."""
    # Explicit ISO range: "from 2026-07-01 to 2026-07-14".
    m = re.search(rf"({_ISO})\s*(?:to|until|till|-|–|—|through)\s*({_ISO})", text)
    if m:
        try:
            return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        except ValueError:
            return None
    # "for a fortnight" -> 14 days from today.
    if "fortnight" in text:
        return today, today + timedelta(days=13)
    # "(for the next) <n> day(s)/week(s)".
    m = re.search(
        r"(?:for\s+)?(?:the\s+)?(?:next\s+)?"
        r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+(day|week)s?",
        text,
    )
    if m:
        n = _count(m.group(1))
        if n:
            days = n * (7 if m.group(2) == "week" else 1)
            return today, today + timedelta(days=days - 1)
    # Single ISO date.
    m = re.search(rf"\b({_ISO})\b", text)
    if m:
        try:
            d = date.fromisoformat(m.group(1))
            return d, d
        except ValueError:
            return None
    # Relative phrases.
    if "next week" in text:
        mon = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return mon, mon + timedelta(days=6)
    if "weekend" in text:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if "next" in text and sat <= today + timedelta(days=1):
            sat += timedelta(days=7)
        return sat, sat + timedelta(days=1)
    if "this week" in text:
        return today, today + timedelta(days=(6 - today.weekday()) % 7)
    if "tomorrow" in text:
        return today + timedelta(days=1), today + timedelta(days=1)
    if _contains(text, "today", "tonight"):
        return today, today
    return None


def extract_context_offline(text: str, today: date) -> ExtractedContext | None:
    """Deterministically extract a context fact; None when unsure (never guesses)."""
    kind = _kind_from(text)
    if kind is None:
        return None
    span = _date_range(text, today)
    if span is None:
        return None
    try:
        return ExtractedContext(kind=kind, start=span[0], end=span[1])
    except ValueError:
        return None


def _describe(entry: ContextEntry, settings: Settings, today: date) -> str:
    when = (
        "active"
        if entry.start_date <= today <= entry.end_date
        else ("upcoming" if entry.start_date > today else "past")
    )
    span = (
        f"{entry.start_date:%d %b %Y}"
        if entry.start_date == entry.end_date
        else f"{entry.start_date:%d %b} – {entry.end_date:%d %b %Y}"
    )
    note = f" — {entry.note}" if entry.note else ""
    return (
        f"  [{entry.id}] {when:<8} {entry.kind:<10} {span}  "
        f"×{entry.factor(settings):.2f}{note}"
    )


async def answer_context_query(settings: Settings, today: date) -> str:
    """List stored context facts (grounded answer for a context question)."""
    async with ContextStore(settings.db_path) as store:
        entries = await store.list_all()
    if not entries:
        return (
            "No context facts are stored. Tell me about a holiday or guests, or use "
            "`ha-spark context add`."
        )
    lines = [f"{len(entries)} context fact(s):"]
    lines += [_describe(e, settings, today) for e in entries]
    return "\n".join(lines)
