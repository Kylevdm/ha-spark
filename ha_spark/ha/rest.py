"""Async Home Assistant REST API client."""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from ha_spark.ha.models import EntityState
from ha_spark.logging import get_logger

log = get_logger(__name__)


class HomeAssistantRestError(RuntimeError):
    """Raised when the Home Assistant REST API returns an error."""


class HomeAssistantRest:
    """Thin async wrapper over the Home Assistant REST API.

    The client owns a shared :class:`httpx.AsyncClient`. Use it as an async
    context manager, or call :meth:`aclose` when done.
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
        )

    async def __aenter__(self) -> HomeAssistantRest:
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

    async def _get(self, path: str) -> Any:
        resp = await self._client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def get_states(self) -> list[EntityState]:
        """Return the current state of every entity."""
        data = await self._get("/states")
        return [EntityState.model_validate(item) for item in data]

    async def get_state(self, entity_id: str) -> EntityState:
        """Return the current state of a single entity."""
        data = await self._get(f"/states/{entity_id}")
        return EntityState.model_validate(data)

    async def get_config(self) -> dict[str, Any]:
        """Return the Home Assistant configuration (version, location, etc.)."""
        data = await self._get("/config")
        return dict(data)

    async def get_services(self) -> list[dict[str, Any]]:
        """Return the catalog of available services, grouped by domain."""
        data = await self._get("/services")
        return list(data)

    async def set_state(
        self, entity_id: str, state: str, attributes: dict[str, Any] | None = None
    ) -> None:
        """Set an entity's state, creating it if it doesn't exist yet."""
        resp = await self._client.post(
            f"/states/{entity_id}",
            json={"state": state, "attributes": attributes or {}},
        )
        resp.raise_for_status()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> list[EntityState]:
        """Call a service and return the list of states changed as a result."""
        log.info("call_service %s.%s data=%s", domain, service, data)
        resp = await self._client.post(f"/services/{domain}/{service}", json=data or {})
        resp.raise_for_status()
        changed = resp.json()
        # HA returns a list of changed states (may be empty).
        return [EntityState.model_validate(item) for item in changed]
