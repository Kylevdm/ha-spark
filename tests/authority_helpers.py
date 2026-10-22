"""Persisted authority fixtures; production permission code is never patched.

Driver tests choose their authority explicitly. This helper never infers a
permission from settings and must not be used to test defaults or implicit grants.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ha_spark import settings_service
from ha_spark.authority import AuthorityStore, OperatingMode
from ha_spark.config import _OPTION_KEYS, _SECRET_OPTION_KEYS, Settings
from ha_spark.devices.execution import ExecutionContext, execution_context
from ha_spark.settings_service import SettingsService

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
    target.planned_generation.set(target.config_generation)
    _contexts.append(target)
    return store


def legacy_settings(
    *, authority_mode: OperatingMode = "observe", **kwargs: Any
) -> Settings:
    """Build settings and seed only the explicitly requested test authority."""
    if "db_path" not in kwargs:
        directory = TemporaryDirectory(prefix="ha-spark-authority-test-")
        _directories.append(directory)
        kwargs["db_path"] = str(Path(directory.name) / "events.db")
    settings = Settings(**kwargs)
    seed_authority(settings, authority_mode)
    return settings


def seeded_execution(settings: Settings) -> ExecutionContext:
    context = ExecutionContext()
    source = execution_context()
    context.authority = source.authority
    context.planned_epoch.set(source.planned_epoch.get())
    context.config_generation = source.config_generation
    context.planned_generation.set(source.planned_generation.get())
    _contexts.append(context)
    return context


def seed_settings(
    directory: Path, inputs: dict[str, Any] | None = None, *, settings: Settings | None = None,
) -> SettingsService:
    """Write a committed pair through the real daemon settings-service path."""
    bootstrap = settings or Settings(supervisor_token="test")
    if inputs is None and settings is not None:
        inputs = {key: getattr(settings, key) for key in settings.model_fields_set
                  if key in _OPTION_KEYS - {"db_path", "log_level", "devices"}}
        inputs = {key: value for key, value in inputs.items()
                  if key not in _SECRET_OPTION_KEYS or value}
        flat = settings.model_dump(exclude={"devices"})
        if settings.devices != Settings(**flat).devices:
            inputs["devices"] = [device.model_dump(mode="json") for device in settings.devices]
    # Test setup represents an existing committed store, not an edit under the
    # test's subsequently chosen operating mode. Keep its authority untouched.
    for name in ("ha_spark_config.json", "ha_spark_secrets.json",
                 "ha_spark_config.initialised", "ha_spark_config.json.prev",
                 "ha_spark_secrets.json.prev"):
        (directory / name).unlink(missing_ok=True)
    service = SettingsService(directory / "options.json", settings=bootstrap, data_dir=directory)
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    # An already-initialised install never seeds; keep the Supervisor out of it.
    supervisor_options = settings_service._supervisor_options
    settings_service._supervisor_options = lambda token: {}
    # Nor does the defaults-seed authority check apply to that setup.
    observe_or_new = settings_service._observe_or_new
    settings_service._observe_or_new = lambda settings: True
    try:
        context.authority = None
        service.load(validate=False, writable=True)
        if inputs:
            service.save(inputs, service.revision, "seed")
            # Setup represents an install that already started with these
            # inputs, so no restart-field change is pending re-check.
            (directory / "ha_spark_config.pending").unlink(missing_ok=True)
            service.load(validate=False, writable=True)
    finally:
        settings_service._supervisor_options = supervisor_options
        settings_service._observe_or_new = observe_or_new
        context.authority = authority
        context.config_generation = generation
    return service


def settings_bytes(directory: Path) -> dict[str, bytes]:
    """Every committed/previous configuration file, for refusal assertions."""
    return {path.name: path.read_bytes() for pattern in ("ha_spark_config*", "ha_spark_secrets*")
            for path in directory.glob(pattern)}


async def run_daemon(settings: Settings, *, poll_seconds: int = 60) -> None:
    """Give phase-2 daemon tests a real committed pair instead of an options overlay."""
    from ha_spark.energy.scheduler import run_forever

    seed_settings(Path(settings.db_path).parent, settings=settings)
    await run_forever(settings, poll_seconds=poll_seconds)


def cleanup_authority() -> None:
    for context in _contexts:
        context.authority = None
        context.planned_epoch.set(None)
        context.planned_generation.set(None)
        context.config_generation = 0
    _contexts.clear()
    for directory in _directories:
        directory.cleanup()
    _directories.clear()
