"""Shared test fixtures."""

from __future__ import annotations

import pytest

from ha_spark.config import Settings


@pytest.fixture(autouse=True)
def _ignore_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's ``.env`` out of every ``Settings()`` built in tests (#212).

    pydantic-settings reads ``env_file`` at construction, so tests behave as in
    a fresh checkout with no ``.env``.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
