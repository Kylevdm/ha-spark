"""Versioned owner-input settings and secrets, committed as one durable revision.

Read-only loads preview first-start legacy options without writing. The daemon
opts into initialisation and saves only after claiming its executor lock. A save
never changes authority: stale work is fenced before publishing the new snapshot.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import ValidationError

from ha_spark.authority import resolve_data_dir
from ha_spark.config import (
    _OPTION_KEYS,
    _SECRET_OPTION_KEYS,
    ConfigError,
    Settings,
    _validated_settings,
    _validation_error_reason,
    actuation_settings_changed,
)
from ha_spark.devices.execution import execution_context
from ha_spark.logging import get_logger
from ha_spark.mode_preconditions import NO_FALLBACK_PLATFORM, fallback_schedule_problem

log = get_logger(__name__)
OPTIONS_PATH = Path("/data/options.json")
_KEYS = _OPTION_KEYS - {"db_path", "log_level"}


class RevisionConflict(ConfigError):
    """The writer's base revision is no longer current."""

    def __init__(self, revision: int) -> None:
        super().__init__("conflict")
        self.revision = revision


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Invalid configuration JSON: line {exc.lineno}, column {exc.colno}"
        ) from None
    except (ValueError, RecursionError):
        raise ConfigError("Invalid configuration JSON") from None
    except (OSError, UnicodeError):
        raise ConfigError("Configuration file unreadable") from None
    if not isinstance(value, dict):
        raise ConfigError("Invalid configuration document")
    return value


def _read_options_overlay(path: Path = OPTIONS_PATH) -> dict[str, Any]:
    """Read the legacy seed vocabulary; never report arbitrary input keys."""
    if not path.exists():
        return {}
    raw = _read_json(path)
    retired = []
    if "proactive_mode" in raw:
        retired.append("proactive_mode")
    devices = raw.get("devices")
    if isinstance(devices, list):
        if any(isinstance(device, dict) and "control" in device for device in devices):
            retired.append("devices.control")
        raw["devices"] = [
            {k: v for k, v in device.items() if k != "control"}
            if isinstance(device, dict)
            else device
            for device in devices
        ]
    if retired:
        log.warning("Ignoring deprecated options: %s", ", ".join(retired))
    unknown = len(raw.keys() - _OPTION_KEYS - {"proactive_mode"})
    if unknown:
        log.warning("Ignoring %d unknown legacy options", unknown)
    return {k: v for k, v in raw.items() if k in _OPTION_KEYS and (v is not None or k == "db_path")}


class SettingsService:
    """One snapshot, masked read view and serialised validate/commit/swap path.

    ``settings`` supplies bootstrap credentials and the fixed database path for
    embedding/tests. ``data_dir`` permits isolated stores. Neither is a save
    input. ``load(writable=True)`` is reserved for the daemon under its lock.
    """

    def __init__(
        self,
        options_path: Path = OPTIONS_PATH,
        *,
        settings: Settings | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.options_path = options_path
        self._bootstrap = settings
        self._settings = settings
        self._directory = data_dir
        self._inputs: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self.revision = 0
        self.save_failed = False
        self._writable = False
        self._lock = RLock()

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            raise RuntimeError("Settings have not been loaded")
        return self._settings

    @settings.setter
    def settings(self, settings: Settings) -> None:
        # Compatibility for tests replacing AppState.settings directly.
        self._settings = settings

    @property
    def data_dir(self) -> Path:
        if self._directory is None:
            raise RuntimeError("Settings have not been loaded")
        return self._directory

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _candidate(
        self, inputs: dict[str, Any], secrets: dict[str, Any], validate: bool
    ) -> Settings:
        base = self._bootstrap
        assert base is not None
        # logging.setLevel otherwise echoes an invalid bootstrap value in its error.
        if base.log_level.upper() not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "WARN",
            "ERROR",
            "CRITICAL",
            "FATAL",
            "NOTSET",
        }:
            raise ConfigError("Invalid log_level")
        bootstrap = {
            key: getattr(base, key)
            for key in (
                "ha_url",
                "ha_token",
                "supervisor_token",
                "db_path",
                "log_level",
            )
        }
        try:
            secret_values = {key: secrets.get(key, "") for key in _SECRET_OPTION_KEYS}
            settings = Settings(**inputs, **secret_values, **bootstrap)
        except ValidationError as exc:
            raise ConfigError(_validation_error_reason(exc)) from None
        if validate:
            if not settings.is_standalone and not settings.supervisor_token:
                raise ConfigError(
                    "No Home Assistant credentials. Running as an HA add-on provides "
                    "SUPERVISOR_TOKEN automatically; for standalone/dev set HA_URL and "
                    "HA_TOKEN (see .env.example)."
                )
            _validated_settings(settings)
        return settings

    def _pair(self, suffix: str = "") -> tuple[int, dict[str, Any], dict[str, Any]]:
        document = _read_json(self._path("ha_spark_config.json" + suffix))
        if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
            raise ConfigError("Unsupported configuration schema_version")
        secrets = _read_json(self._path("ha_spark_secrets.json" + suffix))
        revision = document.get("revision")
        if (
            type(revision) is not int
            or revision < 1
            or secrets.get("revision") != revision
            or type(secrets.get("revision")) is not int
        ):
            raise ConfigError("Configuration revision mismatch")
        inputs, secret_inputs = document.get("settings"), secrets.get("secrets")
        if (
            set(document) != {"schema_version", "revision", "settings"}
            or set(secrets) != {"revision", "secrets"}
            or not isinstance(inputs, dict)
            or not isinstance(secret_inputs, dict)
        ):
            raise ConfigError("Invalid configuration envelope")
        unknown = len(inputs.keys() - (_KEYS - _SECRET_OPTION_KEYS)) + len(
            secret_inputs.keys() - _SECRET_OPTION_KEYS
        )
        if unknown:
            raise ConfigError(f"Unknown configuration fields: {unknown}")
        if any(not isinstance(value, str) for value in secret_inputs.values()):
            raise ConfigError("Invalid secret fields")
        return revision, inputs, secret_inputs

    def load(self, *, validate: bool = True, writable: bool = False) -> Settings:
        """Read a committed pair or preview/commit the one-time legacy seed."""
        with self._lock:
            if self._bootstrap is None:
                try:
                    self._bootstrap = Settings()
                except ValidationError as exc:
                    raise ConfigError(_validation_error_reason(exc)) from None
            if self._directory is None:
                self._directory = resolve_data_dir(self._bootstrap)
            if not self._bootstrap.is_standalone:
                self._bootstrap = self._bootstrap.model_copy(
                    update={
                        "db_path": str(self.data_dir / "ha_spark.db"),
                    }
                )
            marker = self._path("ha_spark_config.initialised")
            recover = False
            if marker.exists():
                # Supervisor still owns log_level, the sole remaining bootstrap option.
                try:
                    level = _read_json(self.options_path).get("log_level")
                except ConfigError:
                    level = None
                if isinstance(level, str):
                    self._bootstrap = self._bootstrap.model_copy(update={"log_level": level})
                try:
                    revision, inputs, secrets = self._pair()
                except ConfigError as current_error:
                    # Do not downgrade an unsupported schema to a previous version.
                    if "schema_version" in str(current_error):
                        raise
                    try:
                        revision, inputs, secrets = self._pair(".prev")
                    except ConfigError:
                        raise current_error from None
                    recover = True
            else:
                raw = _read_options_overlay(self.options_path)
                if isinstance(raw.get("log_level"), str):
                    self._bootstrap = self._bootstrap.model_copy(
                        update={"log_level": raw["log_level"]}
                    )
                if "db_path" in raw and raw["db_path"] != self._bootstrap.db_path:
                    raise ConfigError(
                        "db_path differs from the fixed path; move the database first"
                    )
                inputs, secrets = {}, {}
                for key, value in raw.items():
                    if key not in _KEYS:
                        continue
                    if key in _SECRET_OPTION_KEYS:
                        if value != "":
                            secrets[key] = value
                    elif key == "devices":
                        if value != []:
                            inputs[key] = value
                    else:
                        inputs[key] = value
                revision = 1 if writable else 0
            candidate = self._candidate(inputs, secrets, validate)
            if writable and not validate:
                _validated_settings(candidate)
            if writable and recover:
                try:
                    self._commit(inputs, secrets, revision, backup=False)
                except OSError:
                    self.save_failed = True
                    raise ConfigError("Configuration recovery failed; restart required") from None
            if not marker.exists():
                # Compare validated values: Supervisor choices can encode numeric
                # defaults as strings (notably solar_percentile).
                inputs = {
                    key: value
                    for key, value in inputs.items()
                    if key == "devices"
                    or getattr(candidate, key)
                    != Settings.model_fields[key].get_default(call_default_factory=True)
                }
            if writable and not marker.exists():
                # Validate before touching any remnants from an interrupted seed.
                try:
                    self.data_dir.mkdir(parents=True, exist_ok=True)
                    for name in (
                        "ha_spark_config.json",
                        "ha_spark_secrets.json",
                        "ha_spark_config.json.prev",
                        "ha_spark_secrets.json.prev",
                    ):
                        self._path(name).unlink(missing_ok=True)
                    for pattern in (
                        ".ha_spark_config.json.*",
                        ".ha_spark_secrets.json.*",
                        ".ha_spark_config.initialised.*",
                    ):
                        for path in self.data_dir.glob(pattern):
                            path.unlink()
                    self._commit(inputs, secrets, revision, backup=False)
                    self._write_marker()
                except OSError:
                    self.save_failed = True
                    raise ConfigError(
                        "Configuration initialisation failed; restart required"
                    ) from None
                log.info(
                    "Configuration committed revision=%d fields=%s actor=seed",
                    revision,
                    ",".join(sorted(inputs.keys() | secrets.keys())),
                )
            self._inputs, self._secrets, self.revision = inputs, secrets, revision
            self._settings = candidate
            self._writable = writable
            return candidate

    def _sync_directory(self) -> None:
        fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _temporary(self, name: str, value: dict[str, Any]) -> Path:
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.data_dir)
        path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _commit(
        self, inputs: dict[str, Any], secrets: dict[str, Any], revision: int, *, backup: bool = True
    ) -> None:
        temporary: list[Path] = []
        try:
            temporary.append(
                self._temporary(
                    "ha_spark_config.json",
                    {
                        "schema_version": 1,
                        "revision": revision,
                        "settings": inputs,
                    },
                )
            )
            temporary.append(
                self._temporary(
                    "ha_spark_secrets.json",
                    {
                        "revision": revision,
                        "secrets": secrets,
                    },
                )
            )
            if backup:
                # Hard links preserve the current pair until both backups exist.
                for name in ("ha_spark_config.json", "ha_spark_secrets.json"):
                    previous = self._path(name + ".prev")
                    previous.unlink(missing_ok=True)
                    os.link(self._path(name), previous)
                self._sync_directory()
            os.replace(temporary[1], self._path("ha_spark_secrets.json"))
            os.replace(temporary[0], self._path("ha_spark_config.json"))
            self._sync_directory()
        finally:
            for path in temporary:
                path.unlink(missing_ok=True)

    def _write_marker(self) -> None:
        path = self._temporary("ha_spark_config.initialised", {})
        try:
            os.replace(path, self._path("ha_spark_config.initialised"))
            self._sync_directory()
        finally:
            path.unlink(missing_ok=True)

    def read_view(self) -> dict[str, Any]:
        """The single masking point for effective runtime values."""
        with self._lock:
            out = {key: getattr(self.settings, key) for key in _KEYS}
            out["devices"] = [device.model_dump(mode="json") for device in self.settings.devices]
            for key in _SECRET_OPTION_KEYS:
                out[key] = {"secret": True, "set": bool(getattr(self.settings, key))}
            return {
                "revision": self.revision,
                "settings": out,
                "inputs": sorted(self._inputs.keys() | self._secrets.keys()),
            }

    def save(self, changes: dict[str, Any], expected_revision: int, actor: str) -> dict[str, Any]:
        """Validate a complete candidate, check the revision/guards, commit, fence, swap."""
        with self._lock:
            if self.save_failed:
                raise ConfigError("Configuration save failed; restart required")
            if not self._writable:
                raise ConfigError("Configuration writes require the running daemon")
            for key in ("db_path", "log_level"):
                if key in changes:
                    raise ConfigError(
                        f"{key} cannot be changed by a settings save; bootstrap field"
                    )
            devices = changes.get("devices")
            if "proactive_mode" in changes or (
                isinstance(devices, list)
                and any(isinstance(device, dict) and "control" in device for device in devices)
            ):
                raise ConfigError("retired: use the operating mode")
            unknown = len(changes.keys() - _KEYS)
            if unknown:
                raise ConfigError(f"Unknown configuration fields: {unknown}")
            inputs, secrets = self._inputs.copy(), self._secrets.copy()
            for key, value in changes.items():
                if key in _SECRET_OPTION_KEYS:
                    if value == "***":
                        continue
                    if (
                        value == {"clear": True}
                        and isinstance(value, dict)
                        and value["clear"] is True
                    ):
                        secrets.pop(key, None)
                    elif isinstance(value, str) and value:
                        secrets[key] = value
                    else:
                        raise ConfigError(f"Invalid secret update: {key}")
                elif value is None or (key == "devices" and value == []):
                    inputs.pop(key, None)
                else:
                    inputs[key] = value
            candidate = _validated_settings(self._candidate(inputs, secrets, False))
            if type(expected_revision) is not int or expected_revision != self.revision:
                raise RevisionConflict(self.revision)
            self._guard(candidate)
            changed = sorted(
                key
                for key in inputs.keys()
                | secrets.keys()
                | self._inputs.keys()
                | self._secrets.keys()
                if (inputs | secrets).get(key) != (self._inputs | self._secrets).get(key)
            )
            revision = self.revision + 1
            try:
                self._commit(inputs, secrets, revision)
            except (OSError, ValueError, TypeError):
                self.save_failed = True
                raise ConfigError("Configuration save failed; restart required") from None
            context = execution_context()
            if actuation_settings_changed(self.settings, candidate):
                context.config_generation += 1
            self._inputs, self._secrets, self.revision = inputs, secrets, revision
            self._settings = candidate
            # Actors are channel identities, never arbitrary request body text.
            safe_actor = actor if actor == "seed" else "ingress"
            if actor.startswith("ingress:"):
                user = actor.removeprefix("ingress:")
                if 1 <= len(user) <= 64 and all(
                    c.isascii() and (c.isalnum() or c in "_-") for c in user
                ):
                    safe_actor = actor
            log.info(
                "Configuration committed revision=%d fields=%s actor=%s",
                revision,
                ",".join(changed),
                safe_actor,
            )
            return {"revision": revision, "changed": changed, "status": "live"}

    def _guard(self, candidate: Settings) -> None:
        context = execution_context()
        store = context.authority
        authority = store.state if store is not None and store.known else None
        # Unknown authority fails closed only for a save that would invalidate
        # a currently valid fallback, so an install without one can still save.
        unknown = authority is None or (store is not None and store.save_failed)
        if (unknown and fallback_schedule_problem(self.settings) is None) or (
            authority is not None
            and not unknown
            and (
                authority.committed_mode == "optimise"
                or (authority.transition is not None and authority.transition.target == "optimise")
            )
        ):
            reason = fallback_schedule_problem(candidate)
            if reason == NO_FALLBACK_PLATFORM:
                raise ConfigError("Invalid fallback charging schedule: inverter configuration")
            if reason is not None:
                # Driver reasons may include raw window values. Name only known
                # fields/categories here, never echo candidate input at a surface.
                fields = [
                    name
                    for name in (
                        "solis_fallback_current_a",
                        "max_charge_current_a",
                        "battery_voltage_v",
                        "supply_max_current_a",
                        "supply_voltage_v",
                        "charge_window_start",
                        "charge_window_end",
                    )
                    if name in reason
                ]
                detail = ", ".join(fields) or "charge window or site supply limit"
                raise ConfigError(f"Invalid fallback charging schedule: {detail}")
