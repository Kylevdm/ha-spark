"""Two-tier message router: remote Ollama primary, offline parser fallback.

A fast ``/api/tags`` probe (OLLAMA_HEALTH_TIMEOUT) decides whether the remote
Ollama instance is reachable; if it is, the message goes to ``/api/chat``.
On probe failure, chat failure, or timeout — Ollama is remote (often over
Tailscale) and may be flaky — the deterministic offline parser answers instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ha_spark.config import Settings
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.intent_parser import parse_offline
from ha_spark.logging import get_logger
from ha_spark.ollama import OllamaClient

log = get_logger(__name__)


@dataclass(frozen=True)
class RouterResult:
    """The routed answer and which tier produced it."""

    text: str
    source: Literal["ollama", "offline"]


async def _try_ollama(message: str, settings: Settings) -> str | None:
    """Probe then chat; return the reply, or None if Ollama can't answer."""
    try:
        async with OllamaClient(
            settings.ollama_url, timeout=settings.ollama_health_timeout
        ) as probe:
            await probe.list_models()
    except Exception as exc:  # noqa: BLE001 - any probe failure means fall back
        log.info("Ollama unreachable @ %s (%r); using offline parser", settings.ollama_url, exc)
        return None

    try:
        async with OllamaClient(settings.ollama_url, timeout=settings.ollama_timeout) as client:
            return await client.chat(
                [{"role": "user", "content": message}],
                model=settings.ollama_model,
                num_ctx=settings.ollama_num_ctx,
            )
    except Exception as exc:  # noqa: BLE001 - chat failure also falls back
        log.warning("Ollama chat failed (%r); falling back to offline parser", exc)
        return None


async def route_message(
    message: str, settings: Settings, rest: HomeAssistantRest
) -> RouterResult:
    """Answer ``message`` via Ollama if available, else the offline parser."""
    reply = await _try_ollama(message, settings)
    if reply is not None:
        log.info("Message answered by Ollama (%s)", settings.ollama_model)
        return RouterResult(text=reply, source="ollama")

    result = await parse_offline(message, settings, rest)
    log.info("Message answered by the offline parser (matched=%s)", result.matched)
    return RouterResult(text=result.text, source="offline")
