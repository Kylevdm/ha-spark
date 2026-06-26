"""Bearer-token auth for the agent surface's published port."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

from ha_spark.config import Settings
from ha_spark.logging import get_logger

log = get_logger(__name__)

TOKEN_PATH = Path("/data/agent_token")


def resolve_token(settings: Settings, token_path: Path = TOKEN_PATH) -> str:
    if settings.agent_api_token:
        return settings.agent_api_token
    if token_path.exists():
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)
    log.info("Generated agent API token (printed once): %s", token)
    return token


def verify(header_value: str | None, token: str) -> bool:
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value[len("Bearer ") :]
    return hmac.compare_digest(presented, token)
