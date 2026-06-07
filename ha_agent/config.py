"""Runtime configuration.

Settings are loaded (in precedence order) from environment variables, a local
``.env`` file, and built-in defaults. Home Assistant add-on options
(``/data/options.json``) and the ``SUPERVISOR_TOKEN`` are merged in during
Phase 6; this module keeps the base URLs configurable so standalone and add-on
modes share one code path.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Top-level configuration for ha-agent."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Home Assistant ---
    ha_url: str = Field(default="http://homeassistant.local:8123")
    ha_token: str = Field(default="")
    ha_timeout: float = Field(default=10.0)

    # --- Ollama: primary (large, LAN GPU box) ---
    ollama_lan_url: str = Field(default="http://127.0.0.1:11434")
    ollama_lan_model: str = Field(default="qwen3:14b")
    # --- Ollama: fallback (small, local CPU) ---
    ollama_local_url: str = Field(default="http://127.0.0.1:11434")
    ollama_local_model: str = Field(default="qwen3:4b")
    # Context window; Ollama's default of 2048 truncates agent prompts.
    ollama_num_ctx: int = Field(default=32768)
    ollama_timeout: float = Field(default=120.0)
    ollama_health_timeout: float = Field(default=2.0)

    # --- Storage ---
    db_path: str = Field(default="data/ha_agent.db")

    # --- Logging ---
    log_level: str = Field(default="INFO")

    @property
    def ha_websocket_url(self) -> str:
        """Derive the WebSocket URL from the REST base URL."""
        base = self.ha_url.rstrip("/")
        if base.startswith("https://"):
            ws = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            ws = "ws://" + base[len("http://") :]
        else:
            ws = base
        return f"{ws}/api/websocket"

    @property
    def ha_rest_url(self) -> str:
        """Base URL for the Home Assistant REST API."""
        return f"{self.ha_url.rstrip('/')}/api"


def load_settings() -> Settings:
    """Construct a :class:`Settings` instance from the environment."""
    return Settings()
