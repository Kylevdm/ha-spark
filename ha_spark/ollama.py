"""Minimal async client for a remote Ollama instance.

Only the surface the ``health`` probe needs today: list the models available on
the server via ``/api/tags``. The chat/generate surface is added when the LLM
router lands (Phase 2 proper).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from ha_spark.logging import get_logger

log = get_logger(__name__)


class OllamaClient:
    """Thin async wrapper over a remote Ollama HTTP API.

    Owns a shared :class:`httpx.AsyncClient`; use as an async context manager,
    or call :meth:`aclose` when done.
    """

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        """Return the names of models available on the server (``/api/tags``)."""
        resp = await self._client.get("/api/tags")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return [str(m["name"]) for m in data.get("models", []) if "name" in m]
