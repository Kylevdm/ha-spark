"""Shared test fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ha_spark.config import Settings
from tests.authority_helpers import cleanup_authority


@pytest.fixture(autouse=True)
def _heartbeat_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from ha_spark.api import server

    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))


@pytest.fixture(autouse=True)
def _unbind_authority():
    yield
    cleanup_authority()


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's ``.env`` out of every ``Settings()`` built in tests (#212).

    pydantic-settings reads ``env_file`` at construction, so tests behave as in
    a fresh checkout with no ``.env``.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
