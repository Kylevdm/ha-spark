"""Read and save runtime settings through the legacy add-on options store.

This service owns the active snapshot, options overlay, secret masking and save
checks. A save never changes authority: it only fences stale work before
publishing the newly loaded settings. Persistence remains options.json until
the versioned settings store cutover.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ha_spark.config import (
    _OPTION_KEYS,
    _SECRET_OPTION_KEYS,
    ConfigError,
    Settings,
    _validated_settings,
    actuation_settings_changed,
    validate_options_document,
)
from ha_spark.devices.execution import execution_context
from ha_spark.logging import get_logger
from ha_spark.mode_preconditions import NO_FALLBACK_PLATFORM, fallback_schedule_problem

log = get_logger(__name__)
OPTIONS_PATH = Path("/data/options.json")
# A masked secret submitted back means unchanged, not a replacement value.
_REDACTED = "***"


def _read_options_overlay(path: Path = OPTIONS_PATH) -> dict[str, Any]:
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
    retired = []
    if "proactive_mode" in raw:
        retired.append("proactive_mode")
    devices = raw.get("devices")
    if isinstance(devices, list) and any(
        isinstance(device, dict) and "control" in device for device in devices
    ):
        retired.append("devices.control")
    if retired:
        log.warning("Ignoring deprecated options: %s", ", ".join(retired))
    return {k: v for k, v in raw.items() if k in _OPTION_KEYS and v is not None}


def _load_settings(path: Path, *, validate: bool) -> Settings:
    overlay = _read_options_overlay(path)
    settings = Settings(**overlay)
    if validate and not settings.is_standalone and not settings.supervisor_token:
        raise ConfigError(
            "No Home Assistant credentials. Running as an HA add-on provides "
            "SUPERVISOR_TOKEN automatically; for standalone/dev set HA_URL and "
            "HA_TOKEN (see .env.example)."
        )
    if validate:
        _validated_settings(settings)
    return settings


class SettingsService:
    """One active settings snapshot and the legacy load/read/save interface.

    ``settings`` and ``reload`` may be injected for existing daemon/test wiring.
    Otherwise load overlays the options on environment, .env and defaults, with
    the same credential and tariff validation as startup.
    """

    def __init__(
        self,
        options_path: Path = OPTIONS_PATH,
        *,
        settings: Settings | None = None,
        reload: Callable[[], Settings] | None = None,
    ) -> None:
        self.options_path = options_path
        self._settings = settings
        self.reload = reload or (lambda: _load_settings(self.options_path, validate=True))

    @property
    def settings(self) -> Settings:
        """The active snapshot; load must succeed before it can be read."""
        if self._settings is None:
            raise RuntimeError("Settings have not been loaded")
        return self._settings

    @settings.setter
    def settings(self, settings: Settings) -> None:
        # Compatibility for tests replacing AppState.settings directly.
        self._settings = settings

    def load(self, *, validate: bool = True) -> Settings:
        """Load options; diagnostics may skip credentials and tariff validation."""
        settings = _load_settings(self.options_path, validate=validate)
        self._settings = settings
        return settings

    def read_view(self) -> dict[str, Any]:
        """Return user options with set secrets masked and unset secrets empty."""
        out: dict[str, Any] = {}
        for key in _OPTION_KEYS:
            value = getattr(self.settings, key)
            if key in _SECRET_OPTION_KEYS and value:
                out[key] = _REDACTED
            elif key == "devices":
                # Synthesized DeviceConfig models must remain JSON-safe.
                out[key] = [d.model_dump(mode="json") for d in value]
            else:
                out[key] = value
        return out

    def save(self, updates: dict[str, Any]) -> Settings:
        """Merge known options, validate the whole candidate, persist and reload.

        Unknown keys are ignored and masked secrets mean unchanged. Refusals
        name only fields/categories and leave the file and snapshot unchanged.
        The authority data directory cannot move at runtime (#441); that check
        precedes validation. Actuation changes fence old work before publication.
        """
        if "db_path" in updates and updates["db_path"] != self.settings.db_path:
            raise ConfigError(
                "db_path cannot be changed by a settings save; "
                "set it in the environment or options file and restart"
            )
        current: dict[str, Any] = {}
        if self.options_path.exists():
            current = json.loads(self.options_path.read_text(encoding="utf-8"))
        current.update(
            {
                k: v
                for k, v in updates.items()
                if k in _OPTION_KEYS
                and not (k in _SECRET_OPTION_KEYS and v == _REDACTED)
            }
        )
        # Validate the whole candidate before persisting anything; a rejected
        # save must leave the file byte-identical.
        candidate = validate_options_document(current)
        context = execution_context()
        store = context.authority
        authority = store.state if store is not None and store.known else None
        # Unknown authority fails closed only for a save that would invalidate
        # a currently valid fallback, so an install without one can still save.
        unknown = authority is None or (store is not None and store.save_failed)
        if (
            (unknown and fallback_schedule_problem(self.settings) is None)
            or (authority is not None and not unknown and (
                authority.committed_mode == "optimise"
                or (authority.transition is not None
                    and authority.transition.target == "optimise")
            ))
        ):
            reason = fallback_schedule_problem(candidate)
            if reason == NO_FALLBACK_PLATFORM:
                raise ConfigError("Invalid fallback charging schedule: inverter configuration")
            if reason is not None:
                # Driver reasons may include raw window values. Name only known
                # fields/categories here, never echo candidate input at a surface.
                fields = [name for name in (
                    "solis_fallback_current_a", "max_charge_current_a", "battery_voltage_v",
                    "supply_max_current_a", "supply_voltage_v", "charge_window_start",
                    "charge_window_end",
                ) if name in reason]
                detail = ", ".join(fields) or "charge window or site supply limit"
                raise ConfigError(f"Invalid fallback charging schedule: {detail}")
        self.options_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.options_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        temporary.replace(self.options_path)
        settings = self.reload()
        if actuation_settings_changed(self.settings, settings):
            context.config_generation += 1
        self._settings = settings
        return self.settings
