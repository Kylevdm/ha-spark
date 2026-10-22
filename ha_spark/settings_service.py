"""Versioned owner-input settings and secrets, committed as one durable revision.

Read-only loads preview first-start legacy options without writing; the seed
reads them from ``options.json`` or, when that file holds no legacy key, from
the Supervisor's stored options. The daemon opts into initialisation and
saves only after claiming its executor lock. A save never changes authority:
stale work is fenced before publishing the new snapshot.

A store that can't be trusted (missing or unreadable after the marker, a
mismatched pair with no matching ``*.prev``, invalid values, an unsupported
``schema_version`` or a failed seed) puts the service in configuration repair:
nothing is written or reseeded, reasons name fields only, and the one write
accepted is an import carrying the repair token (a keyed digest of the files
on disk, or ``none``).
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

import httpx
from pydantic import ValidationError

from ha_spark.authority import AuthorityStore, load_authority, resolve_data_dir
from ha_spark.config import (
    _OPTION_KEYS,
    _SECRET_OPTION_KEYS,
    ConfigError,
    Settings,
    _validated_settings,
    _validation_error_reason,
    actuation_settings_changed,
    field_metadata,
)
from ha_spark.devices.execution import execution_context
from ha_spark.logging import get_logger
from ha_spark.mode_preconditions import NO_FALLBACK_PLATFORM, fallback_schedule_problem

log = get_logger(__name__)
OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"
_KEYS = frozenset(field_metadata())
_REPAIR_FILES = (
    "ha_spark_config.json",
    "ha_spark_secrets.json",
    "ha_spark_config.json.prev",
    "ha_spark_secrets.json.prev",
    "ha_spark_config.initialised",
)


class _PairRevisionMismatch(ConfigError):
    """Two valid settings envelopes belong to different revisions."""


class ConfigurationRepair(ConfigError):
    """The committed configuration can't be trusted; only repair may run.

    ``bootstrap`` is the environment-only settings (data directory, HA
    credentials), never a runtime configuration to act on.
    """

    def __init__(self, reasons: list[str], bootstrap: Settings) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons
        self.bootstrap = bootstrap


class RepairTokenConflict(ConfigError):
    """The files changed since the owner read the repair token."""

    def __init__(self, token: str) -> None:
        super().__init__("conflict")
        self.token = token


class _RestartRequired(ConfigError):
    """A persistence failure while loading: a restart, not an import, is the fix."""


class RevisionConflict(ConfigError):
    """The writer's base revision is no longer current."""

    def __init__(self, revision: int) -> None:
        super().__init__("conflict")
        self.revision = revision


def _checked(settings: Settings) -> Settings:
    """Cross-field tariff checks; drop the cause, whose pydantic error holds the input."""
    try:
        return _validated_settings(settings)
    except ConfigError as exc:
        raise ConfigError(str(exc)) from None


def _validated_devices(inputs: dict[str, Any], candidate: Settings) -> dict[str, Any]:
    """Persist devices as validated, so an ignored nested key never reaches the document."""
    if "devices" not in inputs:
        return inputs
    return {**inputs, "devices": [d.model_dump(mode="json") for d in candidate.devices]}


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


def normalise_legacy(
    document: dict[str, Any], fixed_path: str | None = None, *, strict: bool = False,
) -> dict[str, Any]:
    """Shared pure seed/import normalisation; reasons never include input text."""
    raw = document.copy()
    if fixed_path is not None and "db_path" in raw and raw["db_path"] != fixed_path:
        raise ConfigError("db_path differs from the fixed path; move the database first")
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
        if strict:
            raise ConfigError(f"Unknown configuration fields: {unknown}")
        log.warning("Ignoring %d unknown legacy options", unknown)
    return {
        k: v for k, v in raw.items()
        if k in _OPTION_KEYS and (v is not None or k == "db_path")
        and not (k in _SECRET_OPTION_KEYS and v == "")
        and not (k == "devices" and v == [])
    }


def _merged(
    changes: dict[str, Any], inputs: dict[str, Any], secrets: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply a partial update in the service's wire form to copies of the inputs."""
    inputs, secrets = inputs.copy(), secrets.copy()
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
    return inputs, secrets


def _read_options_overlay(path: Path = OPTIONS_PATH) -> dict[str, Any]:
    """Read the legacy seed vocabulary from the Supervisor options file."""
    if not path.exists():
        return {}
    return normalise_legacy(_read_json(path))


def _supervisor_options(token: str) -> dict[str, Any] | None:
    """The add-on's stored options from the Supervisor, or ``None`` if unavailable.

    The response is untrusted, like any other seed source: only a 200 ``ok``
    envelope holding a dict ``data.options`` counts. Nothing about a failure
    is logged: status text, body or error messages can echo the payload or
    the bearer token.
    """
    try:
        # The transport, unlike httpx.Client, logs no request line, so the
        # Supervisor's status text never reaches the log.
        with httpx.HTTPTransport() as transport:
            response = transport.handle_request(httpx.Request(
                "GET", SUPERVISOR_INFO_URL,
                headers={"Authorization": f"Bearer {token}"},
                extensions={"timeout": httpx.Timeout(5.0).as_dict()},
            ))
            try:
                if response.status_code != 200:
                    return None
                body = json.loads(response.read())
            finally:
                response.close()
    except (httpx.HTTPError, ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or body.get("result") != "ok":
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    options = data.get("options")
    return options if isinstance(options, dict) else None


def _observe_or_new(settings: Settings) -> bool:
    """False only when durable authority is, or is moving to, Protect or Optimise.

    Fresh and unknown authority write nothing, and leaving them needs an owner
    transition whose entry checks the configured inverter.
    """
    result = load_authority(settings)
    state = result.state
    if result.status != "known" or state is None:
        return True
    return state.committed_mode == "observe" and (
        state.transition is None or state.transition.target == "observe"
    )


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
        self.pending_restart = False
        self._pending_restart_fields: set[str] = set()
        # Input keys behind the running snapshot, for the pending-restart comparison.
        self._live_inputs: frozenset[str] = frozenset()
        self.request_replan: Callable[[], None] | None = None
        # Called once an import ends configuration repair.
        self.on_repaired: Callable[[], None] | None = None
        self.revision = 0
        self.save_failed = False
        self.repair: list[str] | None = None
        # Keys the repair token so it never exposes a stable digest of the secrets file.
        self._token_key = os.urandom(32)
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
    def bootstrap(self) -> Settings:
        """Environment-only settings; the sole settings available in repair."""
        if self._bootstrap is None:
            raise RuntimeError("Settings have not been loaded")
        return self._bootstrap

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
            _checked(settings)
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
            or type(secrets.get("revision")) is not int
            or secrets["revision"] < 1
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
        if secrets["revision"] != revision:
            raise _PairRevisionMismatch("Configuration revision mismatch")
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
            # Credentials are bootstrap: no configuration import can supply them.
            if validate and not self._bootstrap.is_standalone and (
                not self._bootstrap.supervisor_token
            ):
                raise ConfigError(
                    "No Home Assistant credentials. Running as an HA add-on provides "
                    "SUPERVISOR_TOKEN automatically; for standalone/dev set HA_URL and "
                    "HA_TOKEN (see .env.example)."
                )
            self._writable = writable
            try:
                return self._load(validate=validate, writable=writable)
            except _RestartRequired as exc:
                raise ConfigError(str(exc)) from None
            except ConfigError as exc:
                # Every reason is a service message naming fields, never input text.
                self.repair = [str(exc)]
                self._settings = None
                self._inputs, self._secrets, self.revision = {}, {}, 0
                log.error("Configuration repair: %s", exc)
                raise ConfigurationRepair(self.repair, self.bootstrap) from None

    def _load(self, *, validate: bool, writable: bool) -> Settings:
        """Read the store; any ``ConfigError`` before a write means repair."""
        assert self._bootstrap is not None
        marker = self._path("ha_spark_config.initialised")
        recover = False
        seed_source = "defaults"
        if marker.exists():
            # Supervisor still owns log_level, the sole remaining bootstrap option.
            try:
                options = _read_json(self.options_path) if self.options_path.exists() else {}
            except ConfigError:
                options = {}
            self._apply_log_level(options)
            try:
                revision, inputs, secrets = self._pair()
            except _PairRevisionMismatch as current_error:
                try:
                    revision, inputs, secrets = self._pair(".prev")
                except ConfigError:
                    raise current_error from None
                recover = True
        else:
            raw = _read_options_overlay(self.options_path)
            if raw.keys() - {"log_level"}:
                seed_source = "options"
            elif (
                not self._bootstrap.is_standalone
                and self._bootstrap.supervisor_token
            ):
                # The Supervisor may already have stripped the legacy keys
                # from options.json; its stored options are then the seed
                # source, coerced exactly like the file.
                stored = _supervisor_options(self._bootstrap.supervisor_token)
                if stored is None:
                    log.warning("Supervisor options unavailable; seeding defaults")
                else:
                    legacy = normalise_legacy(stored)
                    if legacy.keys() - {"log_level"}:
                        seed_source = "supervisor"
                    raw = {**raw, **legacy}
            if seed_source == "defaults" and not _observe_or_new(self._bootstrap):
                # Defaults address the documented Solis hub; an install already
                # past Observe must get its own configuration back by import.
                raise ConfigError(
                    "No saved options found to seed from while the operating mode is "
                    "not Observe; import a saved configuration"
                )
            self._apply_log_level(raw)
            raw = normalise_legacy(raw, self._bootstrap.db_path)
            inputs = {key: value for key, value in raw.items()
                      if key in _KEYS - _SECRET_OPTION_KEYS}
            secrets = {key: value for key, value in raw.items() if key in _SECRET_OPTION_KEYS}
            revision = 1 if writable else 0
        candidate = self._candidate(inputs, secrets, validate)
        pending_marker = self._path("ha_spark_config.pending")
        if writable and pending_marker.exists():
            self._guard(candidate, startup=True)
        inputs = _validated_devices(inputs, candidate)
        if writable and not validate:
            _checked(candidate)
        if writable and recover:
            try:
                self._commit(inputs, secrets, revision, backup=False)
            except OSError:
                self.save_failed = True
                raise _RestartRequired("Configuration recovery failed; restart required") from None
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
                raise _RestartRequired(
                    "Configuration initialisation failed; restart required"
                ) from None
            log.info(
                "Configuration committed revision=%d fields=%s actor=seed source=%s",
                revision,
                ",".join(sorted(inputs.keys() | secrets.keys())),
                seed_source,
            )
        if writable and pending_marker.exists():
            try:
                pending_marker.unlink()
                self._sync_directory()
            except OSError:
                raise _RestartRequired("Configuration load failed; restart required") from None
        self._inputs, self._secrets, self.revision = inputs, secrets, revision
        self._settings = candidate
        self._live_inputs = frozenset(inputs)
        self.pending_restart = False
        self._pending_restart_fields.clear()
        self.repair = None
        return candidate

    def _apply_log_level(self, options: dict[str, Any]) -> None:
        """Take the Supervisor's log_level when present; env applies only when absent."""
        assert self._bootstrap is not None
        if "log_level" not in options:
            return
        if not isinstance(options["log_level"], str):
            raise ConfigError("Invalid log_level")
        self._bootstrap = self._bootstrap.model_copy(update={"log_level": options["log_level"]})

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

    def _write_marker(self, name: str = "ha_spark_config.initialised") -> None:
        path = self._temporary(name, {})
        try:
            os.replace(path, self._path(name))
            self._sync_directory()
        finally:
            path.unlink(missing_ok=True)

    def read_view(self) -> dict[str, Any]:
        """The single masking point for effective runtime values."""
        with self._lock:
            if self.repair is not None:
                return {
                    "status": "configuration_repair",
                    "reasons": list(self.repair),
                    "repair_token": self.repair_token(),
                }
            out = {key: getattr(self.settings, key) for key in _KEYS}
            out["devices"] = [device.model_dump(mode="json") for device in self.settings.devices]
            for key in _SECRET_OPTION_KEYS:
                out[key] = {"secret": True, "set": bool(getattr(self.settings, key))}
            return {
                "revision": self.revision,
                "status": "pending_restart" if self.pending_restart else "live",
                "pending_restart_fields": sorted(self._pending_restart_fields),
                "settings": out,
                "inputs": sorted(self._inputs.keys() | self._secrets.keys()),
                "metadata": field_metadata(),
            }

    def repair_token(self) -> str:
        """Keyed digest of the configuration files on disk, or ``none`` when there are none."""
        mac = hmac.new(self._token_key, digestmod="sha256")
        present = False
        for name in _REPAIR_FILES:
            path = self._path(name)
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            except OSError:
                data = b"\0unreadable"
            present = True
            mac.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
        return mac.hexdigest() if present else "none"

    def export(self) -> dict[str, Any]:
        """Return owner inputs only, never secrets or derived defaults."""
        with self._lock:
            return deepcopy(self._inputs)

    def import_document(
        self, document: dict[str, Any], expected: int | str, actor: str,
    ) -> dict[str, Any]:
        """Replace inputs through the same validation, revision and save guards.

        ``expected`` is the revision, or in repair the repair token.
        """
        with self._lock:
            if self.repair is not None:
                return self._repair_import(document, expected)
            raw = normalise_legacy(document, self.settings.db_path, strict=True)
            changes = {key: None for key in self._inputs}
            changes.update({key: value for key, value in raw.items() if key in _KEYS})
            return self.save(changes, expected if isinstance(expected, int) else -1, actor)

    def _repair_import(self, document: dict[str, Any], expected: int | str) -> dict[str, Any]:
        """The one write repair accepts: no trusted snapshot, so no current fallback."""
        if self.save_failed:
            raise ConfigError("Configuration save failed; restart required")
        if not self._writable:
            raise ConfigError("Configuration writes require the running daemon")
        on_disk = {name: self._readable(name) for name in _REPAIR_FILES[:4]}
        for name in ("ha_spark_config.json", "ha_spark_config.json.prev"):
            version = on_disk[name].get("schema_version")
            if type(version) is int and version > 1:
                raise ConfigError("Configuration written by a newer schema_version; not replaced")
        raw = normalise_legacy(document, self.bootstrap.db_path, strict=True)
        # Omitted secrets are preserved from a secrets file that still reads cleanly.
        kept = on_disk["ha_spark_secrets.json"].get("secrets")
        if not isinstance(kept, dict) or not all(
            key in _SECRET_OPTION_KEYS and isinstance(value, str) for key, value in kept.items()
        ):
            kept = {}
        inputs, secrets = _merged(
            {key: value for key, value in raw.items() if key in _KEYS}, {}, kept,
        )
        candidate = _checked(self._candidate(inputs, secrets, False))
        inputs = _validated_devices(inputs, candidate)
        # compare_digest raises on non-ASCII str; compare the fixed token format first.
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}|none", expected) is None
            or not hmac.compare_digest(expected, self.repair_token())
        ):
            raise RepairTokenConflict(self.repair_token())
        self._guard(candidate, startup=True)
        revision = 1 + max(
            (
                value["revision"]
                for value in on_disk.values()
                if type(value.get("revision")) is int and value["revision"] > 0
            ),
            default=0,
        )
        try:
            # The untrusted pair is never kept as a rollback target.
            for name in ("ha_spark_config.json.prev", "ha_spark_secrets.json.prev"):
                self._path(name).unlink(missing_ok=True)
            self._commit(inputs, secrets, revision, backup=False)
            if not self._path("ha_spark_config.initialised").exists():
                self._write_marker()
        except (OSError, ValueError, TypeError):
            self.save_failed = True
            raise ConfigError("Configuration save failed; restart required") from None
        # Nothing was planned in repair, but publishing a snapshot is always fenced.
        execution_context().config_generation += 1
        self._inputs, self._secrets, self.revision = inputs, secrets, revision
        self._settings = candidate
        self._live_inputs = frozenset(inputs)
        self.repair = None
        changed = sorted(inputs.keys() | secrets.keys())
        log.info(
            "Configuration committed revision=%d fields=%s actor=import",
            revision, ",".join(changed),
        )
        if self.on_repaired is not None:
            self.on_repaired()
        return {"revision": revision, "changed": changed, "status": "live"}

    def _readable(self, name: str) -> dict[str, Any]:
        try:
            return _read_json(self._path(name))
        except ConfigError:
            return {}

    def save(self, changes: dict[str, Any], expected_revision: int, actor: str) -> dict[str, Any]:
        """Validate a complete candidate, check the revision/guards, commit, fence, swap."""
        with self._lock:
            if self.save_failed:
                raise ConfigError("Configuration save failed; restart required")
            if self.repair is not None:
                raise ConfigError("Configuration repair: only an import is accepted")
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
            inputs, secrets = _merged(changes, self._inputs, self._secrets)
            candidate = _checked(self._candidate(inputs, secrets, False))
            inputs = _validated_devices(inputs, candidate)
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
            # Pending = the committed pair differs from the running snapshot in a
            # restart field. A synthesized device list follows live entity edits,
            # so `devices` counts only when the running or committed pair sets it.
            restart_fields = {
                key
                for key, meta in field_metadata().items()
                if meta["reload"] == "restart"
                and (key != "devices" or "devices" in inputs.keys() | self._live_inputs)
                and getattr(self.settings, key) != getattr(candidate, key)
            }
            pending_restart = bool(restart_fields)
            revision = self.revision + 1
            try:
                # Durable before the pair: a crash may cause an extra check, never a missed one.
                if pending_restart:
                    self._write_marker("ha_spark_config.pending")
                self._commit(inputs, secrets, revision)
            except (OSError, ValueError, TypeError):
                self.save_failed = True
                raise ConfigError("Configuration save failed; restart required") from None
            self.pending_restart = pending_restart
            self._pending_restart_fields = restart_fields
            self._inputs, self._secrets, self.revision = inputs, secrets, revision
            if not self.pending_restart:
                context = execution_context()
                if actuation_settings_changed(self.settings, candidate):
                    context.config_generation += 1
                self._settings = candidate
                self._live_inputs = frozenset(inputs)
                if self.request_replan is not None:
                    try:
                        self.request_replan()
                    except Exception:
                        log.warning("Configuration replan request unavailable")
            # Actors are channel identities, never arbitrary request body text.
            safe_actor = actor if actor in {"seed", "cli", "import"} else "ingress"
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
            return {
                "revision": revision, "changed": changed,
                "status": "pending_restart" if self.pending_restart else "live",
            }

    def _guard(self, candidate: Settings, *, startup: bool = False) -> None:
        context = execution_context()
        # Startup reads durable authority without initialising, saving or fencing it.
        store = AuthorityStore(candidate) if startup else context.authority
        authority = store.state if store is not None and store.known else None
        # Unknown authority fails closed only for a save that would invalidate
        # a currently valid fallback, so an install without one can still save.
        unknown = authority is None or (store is not None and store.save_failed)
        if (unknown and not startup and fallback_schedule_problem(self.settings) is None) or (
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
