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
from typing import Any, Literal

from pydantic import Field, field_validator
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
        # Energy planner knobs.
        "proactive_mode",
        "battery_capacity_kwh",
        "min_soc",
        "target_soc_cap",
        "max_charge_current_a",
        "charge_buffer_pct",
        "charge_strategy",
        "solar_haircut_k",
        "forecast_days",
        "expected_load_kwh",
        "charge_window_start",
        "charge_window_end",
        # Energy planner v2: tariff, slot profile, Octopus consumption pull.
        # The API key is exposed here too — options.json is the only user config
        # surface in add-on mode (the add-on schema will mark it `password`).
        "rate_offpeak_gbp_kwh",
        "rate_peak_gbp_kwh",
        "rate_export_gbp_kwh",
        "charge_efficiency",
        "solar_percentile",
        "profile_min_days",
        "profile_history_days",
        "timezone",
        "plan_run_time",
        "backfill_source_entity",
        "octopus_api_key",
        "octopus_mpan",
        "octopus_meter_serial",
        "octopus_api_url",
        # Battery model fallback.
        "battery_voltage_v",
        # Entity IDs: exposed so other installs can map their own sensors/controls
        # (the code defaults match the author's setup).
        "soc_entity",
        "battery_voltage_entity",
        "solar_tomorrow_entity",
        "octopus_rate_entity",
        "dispatch_entity",
        "ev_plug_entity",
        "ev_status_entity",
        "consumption_energy_entity",
        "grid_power_entity",
        "supply_max_current_a",
        "supply_voltage_v",
        "charge_current_entity",
        "inverter_power_switch_entity",
        "ha_template_charge_needed_entity",
        # Forecast ledger: signal sampling (Phase 6A).
        "person_entities",
        "heatpump_energy_entity",
        "outdoor_weather_entity",
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

    # --- Energy planner ---
    # Proactivity: "off" (compute only), "simulate" (log intended writes, default),
    # "on" (real control — not exercised in v1).
    proactive_mode: Literal["off", "simulate", "on"] = Field(default="simulate")

    # Battery / inverter model.
    battery_capacity_kwh: float = Field(default=26.88)
    battery_voltage_v: float = Field(default=51.0)  # fallback if the sensor is unavailable
    min_soc: float = Field(default=20.0)
    target_soc_cap: float = Field(default=90.0)
    max_charge_current_a: float = Field(default=62.5)
    # Safety margin applied to the forecast deficit before sizing the charge.
    charge_buffer_pct: float = Field(default=20.0)
    # Round-trip AC->DC->AC efficiency: the planner buys required/efficiency.
    charge_efficiency: float = Field(default=0.90)
    # Overnight sizing: "deficit" buys only the forecast shortfall;
    # "fill" charges to target_soc_cap every night (wins once export > off-peak).
    charge_strategy: Literal["deficit", "fill"] = Field(default="deficit")

    # Forecast / model coefficients.
    solar_haircut_k: float = Field(default=1.0)
    # Solcast percentile to plan on: 50 (median), 10 (conservative), 90 (optimistic).
    solar_percentile: Literal[10, 50, 90] = Field(default=50)
    forecast_days: int = Field(default=14)
    expected_load_kwh: float = Field(default=24.0)  # fallback when statistics unavailable

    # Fixed cheap charge window (local HH:MM).
    charge_window_start: str = Field(default="23:30")
    charge_window_end: str = Field(default="05:30")

    # Two-rate tariff (GBP/kWh): off-peak inside the window/dispatch slots, else peak.
    rate_offpeak_gbp_kwh: float = Field(default=0.069)
    rate_peak_gbp_kwh: float = Field(default=0.30)
    # Export/feed-in rate (GBP/kWh); 0 disables export revenue in cost projections.
    rate_export_gbp_kwh: float = Field(default=0.0)

    # v2 slot-profile load model (from imported Octopus half-hourly consumption).
    profile_min_days: int = Field(default=7)
    profile_history_days: int = Field(default=60)
    timezone: str = Field(default="Europe/London")

    # Local time (HH:MM) at which `ha-spark run` computes/applies the daily plan.
    plan_run_time: str = Field(default="22:00")

    # Statistic whose history seeds `ha-spark backfill-load` (a true-load power
    # or energy sensor); the CLI's --from flag overrides it.
    backfill_source_entity: str = Field(default="")

    # Octopus REST API (for `pull-consumption`; CSV import needs none of these).
    octopus_api_key: str = Field(default="")
    octopus_mpan: str = Field(default="")
    octopus_meter_serial: str = Field(default="")
    octopus_api_url: str = Field(default="https://api.octopus.energy/v1")

    # HA entity IDs (all overridable; defaults match the user's current setup).
    soc_entity: str = Field(default="sensor.solisac_battery_soc")
    battery_voltage_entity: str = Field(default="sensor.solisac_battery_voltage")
    solar_tomorrow_entity: str = Field(default="sensor.solcast_pv_forecast_forecast_tomorrow")
    octopus_rate_entity: str = Field(
        default="sensor.octopus_energy_electricity_22l4386358_2200012282082_current_rate"
    )
    dispatch_entity: str = Field(
        default="binary_sensor.octopus_energy_00000000_0009_4000_8020_000000068b29_intelligent_dispatching"  # noqa: E501
    )
    ev_plug_entity: str = Field(default="sensor.myenergi_zappi_22300254_plug_status")
    ev_status_entity: str = Field(default="sensor.myenergi_zappi_22300254_status")
    # True house load excluding battery/EV; historic_household_usage backfills
    # hourly stats to Dec 2025 (vs ~Jun 2026 for sensor.house_consumption_energy).
    consumption_energy_entity: str = Field(default="sensor.historic_household_usage")
    # Live supply guard: throttle battery charging while whole-house AC draw
    # exceeds supply_max_current_a. Disabled while grid_power_entity is empty.
    grid_power_entity: str = Field(default="")  # W; whole-house grid/supply draw
    supply_max_current_a: float = Field(default=75.0)
    supply_voltage_v: float = Field(default=240.0)  # AC volts, converts W -> A
    charge_current_entity: str = Field(default="number.solisac_timed_charge_current")
    inverter_power_switch_entity: str = Field(default="select.solisac_power_switch")
    ha_template_charge_needed_entity: str = Field(default="sensor.charge_energy_needed")

    # Forecast ledger signal sampling (Phase 6A): recorded so training data
    # accumulates ahead of the models (6B+) that will consume it.
    # Comma-separated person/device_tracker entity ids; empty disables occupancy sampling.
    person_entities: str = Field(default="")
    # Dedicated heat-pump energy sensor (kWh); empty disables heatpump_kwh sampling.
    heatpump_energy_entity: str = Field(default="")
    # Weather entity with a `temperature` attribute (e.g. HA's built-in Met.no `weather.home`).
    outdoor_weather_entity: str = Field(default="weather.home")

    @field_validator("solar_percentile", mode="before")
    @classmethod
    def _coerce_solar_percentile(cls, v: object) -> object:
        # The add-on schema `list(10|50|90)` delivers the choice as a string in
        # /data/options.json; the field is an int Literal.
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v

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


def load_settings(*, validate: bool = True) -> Settings:
    """Build :class:`Settings`, overlaying add-on options, and validate.

    Add-on options (``/data/options.json``) take precedence over environment
    variables. In add-on mode a ``SUPERVISOR_TOKEN`` is required; standalone/dev
    mode (``HA_URL`` + ``HA_TOKEN`` set) bypasses that requirement.

    ``validate=False`` skips the credential check (but keeps the options
    overlay) for diagnostic paths like ``ha-spark health`` that must run and
    report rather than fail fast.
    """
    overlay = _read_options_overlay()
    settings = Settings(**overlay)
    if validate and not settings.is_standalone and not settings.supervisor_token:
        raise ConfigError(
            "No Home Assistant credentials. Running as an HA add-on provides "
            "SUPERVISOR_TOKEN automatically; for standalone/dev set HA_URL and "
            "HA_TOKEN (see .env.example)."
        )
    return settings
