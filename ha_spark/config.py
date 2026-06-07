"""Runtime configuration.

ha-spark targets the Home Assistant add-on runtime. In add-on mode the HA REST
and WebSocket endpoints are reached through the Supervisor proxy
(``http://supervisor/core/api`` / ``ws://supervisor/core/websocket``) and
authenticated with ``SUPERVISOR_TOKEN``; user-exposed options come from
``/data/options.json``. For local development a standalone escape hatch is kept:
set ``HA_URL`` + ``HA_TOKEN`` and the same code paths talk to an HA instance
directly.

Settings load (in precedence order) from add-on options (``/data/options.json``),
environment variables, a local ``.env`` file, and built-in defaults.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ha_spark.logging import get_logger

log = get_logger(__name__)

# Add-on mode: HA Core is reached through the Supervisor proxy.
SUPERVISOR_REST_URL = "http://supervisor/core/api"
SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"

# User-exposed add-on options we honour from /data/options.json.
_OPTIONS_PATH = Path("/data/options.json")
_OPTION_KEYS = frozenset(
    {
        "ollama_url",
        "ollama_model",
        "ollama_num_ctx",
        "ollama_timeout",
        "ollama_health_timeout",
        "ha_timeout",
        "db_path",
        "log_level",
    }
)


class ConfigError(RuntimeError):
    """Raised when the runtime configuration is invalid or incomplete."""


class Settings(BaseSettings):
    """Top-level configuration for ha-spark."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Home Assistant ---
    # Add-on mode is the default (URLs/token derived from the Supervisor).
    # Setting both ha_url and ha_token switches to standalone/dev mode.
    ha_url: str = Field(default="")
    ha_token: str = Field(default="")
    supervisor_token: str = Field(default="")
    ha_timeout: float = Field(default=10.0)

    # --- Ollama (single remote tier, typically reached over Tailscale) ---
    ollama_url: str = Field(default="http://127.0.0.1:11434")
    ollama_model: str = Field(default="qwen3:14b")
    # Context window; Ollama's default of 2048 truncates agent prompts.
    ollama_num_ctx: int = Field(default=32768)
    ollama_timeout: float = Field(default=120.0)
    ollama_health_timeout: float = Field(default=2.0)

    # --- Storage ---
    db_path: str = Field(default="data/ha_spark.db")

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @property
    def is_standalone(self) -> bool:
        """True when an explicit HA URL + token override add-on mode (dev)."""
        return bool(self.ha_url and self.ha_token)

    @property
    def auth_token(self) -> str:
        """The token used to authenticate to HA: dev token, else Supervisor."""
        return self.ha_token or self.supervisor_token

    @property
    def ha_rest_url(self) -> str:
        """Base URL for the Home Assistant REST API."""
        if self.is_standalone:
            return f"{self.ha_url.rstrip('/')}/api"
        return SUPERVISOR_REST_URL

    @property
    def ha_websocket_url(self) -> str:
        """WebSocket URL for the Home Assistant API."""
        if not self.is_standalone:
            return SUPERVISOR_WS_URL
        base = self.ha_url.rstrip("/")
        if base.startswith("https://"):
            ws = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            ws = "ws://" + base[len("http://") :]
        else:
            ws = base
        return f"{ws}/api/websocket"


def _read_options_overlay(path: Path = _OPTIONS_PATH) -> dict[str, Any]:
    """Read user-exposed add-on options from ``/data/options.json``, if present."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("Could not read add-on options at %s; ignoring", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in _OPTION_KEYS and v is not None}


def load_settings() -> Settings:
    """Build :class:`Settings`, overlaying add-on options, and validate.

    Add-on options (``/data/options.json``) take precedence over environment
    variables. In add-on mode a ``SUPERVISOR_TOKEN`` is required; standalone/dev
    mode (``HA_URL`` + ``HA_TOKEN`` set) bypasses that requirement.
    """
    overlay = _read_options_overlay()
    settings = Settings(**overlay)
    if not settings.is_standalone and not settings.supervisor_token:
        raise ConfigError(
            "No Home Assistant credentials. Running as an HA add-on provides "
            "SUPERVISOR_TOKEN automatically; for standalone/dev set HA_URL and "
            "HA_TOKEN (see .env.example)."
        )
    return settings
