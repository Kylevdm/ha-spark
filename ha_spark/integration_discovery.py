"""Per-boot companion credentials and Supervisor discovery, in daemon memory only.

The raw async transport deliberately bypasses httpx's request/status logging.
Failures expose only their type, never Supervisor payloads or exception text.
Announcement is independent of planning; cancel it before shutdown safe-state,
then withdraw once after the hardware work has finished.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from uuid import UUID, uuid4

import httpx

log = logging.getLogger(__name__)
_DISCOVERY_URL = "http://supervisor/discovery"


class IntegrationDiscovery:
    """One boot identity, shared across repair and normal listener lifetimes."""

    def __init__(
        self, *, supervisor_token: str, host: str,
        auth_token: str | None = None, session_id: str | None = None,
    ) -> None:
        self.auth_token = auth_token if auth_token is not None else secrets.token_urlsafe(32)
        self.session_id = session_id if session_id is not None else str(uuid4())
        self._supervisor_token = supervisor_token
        self._host = host
        self._message_id: str | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start once a listener is serving; repair handover must not repost."""
        if self._task is None:
            self._task = asyncio.create_task(self._announce())

    async def cancel_announcement(self) -> None:
        """Join cancellation, including a post in flight, before any withdrawal."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _request(self, method: str, url: str, body: object = None) -> object:
        async with httpx.AsyncHTTPTransport() as transport:
            request = httpx.Request(
                method, url,
                headers={"Authorization": f"Bearer {self._supervisor_token}"},
                json=body,
                extensions={"timeout": httpx.Timeout(3).as_dict()},
            )
            response = await transport.handle_async_request(request)
            try:
                if response.status_code != 200:
                    raise ValueError
                if method == "DELETE":
                    return None
                return json.loads(await response.aread())
            finally:
                await response.aclose()

    async def _announce(self) -> None:
        delay = 1
        while True:
            try:
                async with asyncio.timeout(3):
                    body = await self._request("POST", _DISCOVERY_URL, {
                        "service": "ha_spark",
                        "config": {
                            "host": self._host, "port": 8099,
                            "auth_token": self.auth_token, "session_id": self.session_id,
                        },
                    })
                if not isinstance(body, dict) or body.get("result") != "ok":
                    raise ValueError
                data = body.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("uuid"), str):
                    raise ValueError
                # Normalised, so nothing but hex reaches the DELETE URL.
                self._message_id = UUID(data["uuid"]).hex
                return
            except Exception as exc:  # noqa: BLE001 - only the type is ever logged
                log.warning("Supervisor discovery failed: %s", type(exc).__name__)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def withdraw(self) -> None:
        """One bounded attempt, called only after the shutdown safe-state."""
        if self._message_id is None:
            return
        try:
            async with asyncio.timeout(3):
                await self._request("DELETE", f"{_DISCOVERY_URL}/{self._message_id}")
        except Exception as exc:  # noqa: BLE001 - only the type is ever logged
            log.warning("Supervisor discovery withdrawal failed: %s", type(exc).__name__)
