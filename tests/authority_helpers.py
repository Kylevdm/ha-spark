"""Persisted authority fixtures; production permission code is never patched.

The migration adapter preserves the intent of legacy driver tests: former real
write cases explicitly receive Optimise, and *all* retired-gate cases receive
Observe. It must not be used by tests of defaults or implicit grants.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ha_spark.authority import AuthorityStore, OperatingMode
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext, execution_context

_directories: list[TemporaryDirectory[str]] = []
_contexts: list[ExecutionContext] = []


def seed_authority(
    settings: Settings, mode: OperatingMode, *, epoch: int = 1,
    context: ExecutionContext | None = None,
) -> AuthorityStore:
    """Seed store + marker in a private temporary directory and bind a planned epoch."""
    standalone = settings
    if not settings.is_standalone:
        directory = TemporaryDirectory(prefix="ha-spark-authority-test-")
        _directories.append(directory)
        standalone = settings.model_copy(update={
            "ha_url": "http://ha.test", "ha_token": "test",
            "db_path": str(Path(directory.name) / "events.db"),
        })
    store = AuthorityStore(standalone)
    store.initialise()
    assert store.state is not None
    store.save(store.state.model_copy(update={"committed_mode": mode, "epoch": epoch}))
    target = context or execution_context()
    target.authority = store
    target.planned_epoch.set(epoch)
    _contexts.append(target)
    return store


def legacy_settings(
    *, authority_mode: OperatingMode | None = None, **kwargs: Any
) -> Settings:
    """Map old test setup to explicit authority, never a production permission fallback."""
    if "db_path" not in kwargs:
        directory = TemporaryDirectory(prefix="ha-spark-authority-test-")
        _directories.append(directory)
        kwargs["db_path"] = str(Path(directory.name) / "events.db")
    settings = Settings(**kwargs)
    mode: OperatingMode = "observe"
    if settings.proactive_mode == "on" and all(
        device.control == "ha_spark" for device in settings.devices if device.type == "inverter"
    ):
        mode = "optimise"
    seed_authority(settings, authority_mode or mode)
    return settings


def seeded_execution(settings: Settings) -> ExecutionContext:
    context = ExecutionContext()
    source = execution_context()
    context.authority = source.authority
    context.planned_epoch.set(source.planned_epoch.get())
    _contexts.append(context)
    return context


def cleanup_authority() -> None:
    for context in _contexts:
        context.authority = None
        context.planned_epoch.set(None)
    _contexts.clear()
    for directory in _directories:
        directory.cleanup()
    _directories.clear()
