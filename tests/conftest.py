"""Shared test fixtures."""

from __future__ import annotations

import pytest

from ha_spark.config import Settings
from tests.authority_helpers import cleanup_authority


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


@pytest.fixture(scope="module")
def legacy_solis_hub():
    """Run a module written against the pre-0.31 default hub `solis_control` (#567).

    0.31 defaults `solis_control_hub` to empty; these tests exercise the legacy
    overlay path, so they get its hub as the default rather than in every call.
    """
    field = Settings.model_fields["solis_control_hub"]
    default = field.default
    field.default = "solis_control"
    Settings.model_rebuild(force=True)
    yield
    field.default = default
    Settings.model_rebuild(force=True)
