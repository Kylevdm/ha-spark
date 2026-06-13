"""Two-tier message router: remote Ollama primary, offline parser fallback.

A fast ``/api/tags`` probe (OLLAMA_HEALTH_TIMEOUT) decides whether the remote
Ollama instance is reachable; if it is, the message goes to ``/api/chat``.
On probe failure, chat failure, or timeout — Ollama is remote (often over
Tailscale) and may be flaky — the deterministic offline parser answers instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ha_spark.config import Settings
from ha_spark.context_intent import (
    ExtractedContext,
    build_extraction_messages,
    parse_llm_extraction,
    record_context,
)
from ha_spark.copilot import build_grounding, grounded_system_prompt
from ha_spark.energy.forecast import load_timezone
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.intent_parser import (
    answer_context_query,
    extract_context_offline,
    is_context_query,
    mentions_context,
    parse_offline,
)
from ha_spark.logging import get_logger
from ha_spark.ollama import OllamaClient

log = get_logger(__name__)


@dataclass(frozen=True)
class RouterResult:
    """The routed answer and which tier produced it."""

    text: str
    source: Literal["ollama", "offline"]


async def _ollama_reachable(settings: Settings) -> bool:
    """Fast ``/api/tags`` probe: is the remote Ollama instance answering?"""
    try:
        async with OllamaClient(
            settings.ollama_url, timeout=settings.ollama_health_timeout
        ) as probe:
            await probe.list_models()
        return True
    except Exception as exc:  # noqa: BLE001 - any probe failure means fall back
        log.info("Ollama unreachable @ %s (%r); using offline parser", settings.ollama_url, exc)
        return False


async def _grounded_chat(
    message: str, settings: Settings, rest: HomeAssistantRest
) -> str | None:
    """Probe, then answer via Ollama grounded in the live plan; None to fall back.

    The probe runs first so the grounding plan isn't computed when Ollama is
    unreachable. Grounding is best-effort: if it can't be built the model still
    answers (told the plan is unavailable) rather than failing the chat.
    """
    if not await _ollama_reachable(settings):
        return None
    grounding = await build_grounding(settings, rest)
    system = grounded_system_prompt(grounding)
    try:
        async with OllamaClient(settings.ollama_url, timeout=settings.ollama_timeout) as client:
            return await client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                model=settings.ollama_model,
                num_ctx=settings.ollama_num_ctx,
            )
    except Exception as exc:  # noqa: BLE001 - chat failure also falls back
        log.warning("Ollama chat failed (%r); falling back to offline parser", exc)
        return None


async def _ollama_extract(message: str, settings: Settings, today: datetime) -> str | None:
    """Run the extraction pass on Ollama; None when it can't give a usable reply."""
    try:
        async with OllamaClient(
            settings.ollama_url, timeout=settings.ollama_health_timeout
        ) as probe:
            await probe.list_models()
    except Exception as exc:  # noqa: BLE001 - unreachable -> offline extraction
        log.info("Ollama unreachable for extraction (%r); using offline parser", exc)
        return None
    try:
        async with OllamaClient(settings.ollama_url, timeout=settings.ollama_timeout) as client:
            return await client.chat(
                build_extraction_messages(message, today.date()),
                model=settings.ollama_model,
                num_ctx=settings.ollama_num_ctx,
            )
    except Exception as exc:  # noqa: BLE001 - chat failure -> offline extraction
        log.warning("Ollama extraction failed (%r); using offline parser", exc)
        return None


async def _extract_context(
    message: str, settings: Settings
) -> tuple[ExtractedContext | None, Literal["ollama", "offline"]]:
    """Extract a context fact via Ollama if reachable, else the offline parser.

    A reachable model's verdict is trusted even when it declines (returns
    ``None``) — its judgement beats the regex parser, so we don't second-guess
    it offline.
    """
    today = datetime.now(load_timezone(settings.timezone))
    raw = await _ollama_extract(message, settings, today)
    if raw is not None:
        return parse_llm_extraction(raw), "ollama"
    return extract_context_offline(message.lower(), today.date()), "offline"


async def _try_context_intent(message: str, settings: Settings) -> RouterResult | None:
    """Handle a context query or a context-setting statement; None to fall through.

    Context only ever records reviewable facts (never actuates hardware), and
    runs before plain chat so a durable planner instruction isn't lost to a
    chat reply.
    """
    text = message.lower()
    if is_context_query(text):
        today = datetime.now(load_timezone(settings.timezone)).date()
        return RouterResult(text=await answer_context_query(settings, today), source="offline")
    if not mentions_context(text):
        return None
    extracted, source = await _extract_context(message, settings)
    if extracted is None:
        return None
    confirmation = await record_context(settings, extracted, source=source)
    log.info("Recorded context fact via %s: %s %s..%s", source, extracted.kind,
             extracted.start, extracted.end)
    return RouterResult(text=confirmation, source=source)


async def route_message(
    message: str, settings: Settings, rest: HomeAssistantRest
) -> RouterResult:
    """Answer ``message``: context intent first, then Ollama chat, else offline."""
    context = await _try_context_intent(message, settings)
    if context is not None:
        return context

    reply = await _grounded_chat(message, settings, rest)
    if reply is not None:
        log.info("Message answered by Ollama (%s)", settings.ollama_model)
        return RouterResult(text=reply, source="ollama")

    result = await parse_offline(message, settings, rest)
    log.info("Message answered by the offline parser (matched=%s)", result.matched)
    return RouterResult(text=result.text, source="offline")
