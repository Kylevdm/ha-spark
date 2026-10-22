"""Runtime configuration.

ha-spark targets the Home Assistant add-on runtime. In add-on mode the HA REST
and WebSocket endpoints are reached through the Supervisor proxy
(``http://supervisor/core/api`` / ``ws://supervisor/core/websocket``) and
authenticated with ``SUPERVISOR_TOKEN``; owner inputs come from the committed
settings document and secrets file. For local development a standalone escape hatch is kept:
set ``HA_URL`` + ``HA_TOKEN`` and the same code paths talk to an HA instance
directly.

The settings service loads the committed pair, or previews legacy options on
first start. Environment variables and a local ``.env`` supply only mode-specific
bootstrap values, never runtime settings.
"""

from __future__ import annotations

from datetime import time
from typing import Annotated, Any, Literal, TypedDict, cast

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Add-on mode: HA Core is reached through the Supervisor proxy.
SUPERVISOR_REST_URL = "http://supervisor/core/api"
SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"

# Frozen 0.24 legacy seed/import vocabulary, not the runtime or Supervisor schema.
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
        "battery_capacity_kwh",
        "min_soc",
        "target_soc_cap",
        "max_charge_current_a",
        "solis_fallback_current_a",
        "battery_discharge_ceiling_kw",
        "dno_export_limit_kw",
        "charge_buffer_pct",
        "charge_strategy",
        "solar_haircut_k",
        "forecast_days",
        "expected_load_kwh",
        "charge_window_start",
        "charge_window_end",
        # Energy planner v2: tariff, slot profile, Octopus consumption pull.
        "rate_offpeak_gbp_kwh",
        "rate_peak_gbp_kwh",
        "rate_export_gbp_kwh",
        "tariff_provider",
        "export_event_provider",
        "dynamic_rates_entity",
        "dynamic_rates_entity_tomorrow",
        "charge_efficiency",
        "solar_percentile",
        "profile_min_days",
        "profile_history_days",
        "timezone",
        "plan_run_time",
        "backfill_source_entity",
        # Derived base-load (ADR-0001): per-component statistic IDs + invert flags.
        # Grid import is required when any of these are set; the others are
        # optional and contribute zero with a degradation note when unset.
        "derive_grid_import_entity",
        "derive_grid_export_entity",
        "derive_solar_generation_entity",
        "derive_battery_charge_entity",
        "derive_battery_discharge_entity",
        "derive_ev_charge_entity",
        "derive_invert_grid_import",
        "derive_invert_grid_export",
        "derive_invert_solar_generation",
        "derive_invert_battery_charge",
        "derive_invert_battery_discharge",
        "derive_invert_ev_charge",
        "octopus_api_key",
        "octopus_mpan",
        "octopus_meter_serial",
        "octopus_api_url",
        "octopus_account_number",
        "octopus_product_code",
        "octopus_tariff_code",
        "axle_api_key",
        "axle_api_url",
        "axle_event_entity",
        "axle_event_rate_gbp_kwh",
        "notify_service",
        # Battery model fallback.
        "battery_voltage_v",
        # Entity IDs: exposed so other installs can map their own sensors/controls
        # (the code defaults match the author's setup).
        "soc_entity",
        "soc_max_report_age_minutes",
        "soc_failure_threshold",
        "soc_recovery_minutes",
        "battery_voltage_entity",
        "battery_power_entity",
        "solar_tomorrow_entity",
        "solar_today_entity",
        "octopus_rate_entity",
        "dispatch_entity",
        "ev_plug_entity",
        "ev_status_entity",
        "ev_power_entity",
        "dispatch_grid_power_entity",
        "dispatch_grid_power_invert",
        "hold_degraded_minutes",
        "readiness_window_days",
        "readiness_mae_improvement_pct",
        "readiness_max_cost_delta",
        "readiness_min_guard_coverage_pct",
        "ev_status_max_age_seconds",
        "ev_plug_max_age_seconds",
        "ev_power_max_age_seconds",
        "live_dispatch_max_age_seconds",
        "adjusted_rate_max_age_seconds",
        "dispatch_grid_power_max_age_seconds",
        "battery_power_max_age_seconds",
        "octopus_dispatch_max_age_seconds",
        "ev_liveness_entity",
        "dispatch_liveness_entity",
        "battery_power_discharge_sign",
        "consumption_energy_entity",
        "grid_power_entity",
        "supply_max_current_a",
        "supply_voltage_v",
        "charge_current_entity",
        "inverter_power_switch_entity",
        "ha_template_charge_needed_entity",
        # Inverter selector + AlphaESS control (Task 3).
        "inverter",
        "solis_model_confirmed",
        "solis_control_hub",
        "solis_modbus_slave",
        "inverter_clock_tolerance_minutes",
        "inverter_clock_dst_sync",
        "alphaess_serial",
        # Structured device config (Phase 7): list of controllable devices.
        "devices",
        # Forecast ledger: signal sampling (Phase 6A).
        "person_entities",
        "heatpump_energy_entity",
        "outdoor_weather_entity",
        # Weather-aware ML load model (Phase 6B).
        "load_model",
        "buffer_mode",
        "latitude",
        "longitude",
        # Context store (Phase 6C).
        "away_load_factor",
        "guests_load_factor",
        # Agent surface (MCP + OpenAPI).
        "agent_surface",
        "agent_exposure",
        "agent_api_token",
        "agent_expose_port",
        # V2L observe + tally + notify.
        "v2l_power_entity",
        "v2l_round_trip_efficiency",
        "v2l_peak_rate_gbp",
        "v2l_offpeak_rate_gbp",
        "v2l_cutoff_time",
        "v2l_notify_service",
        "v2l_budget_kwh",
        "v2l_max_rate_kw",
        "v2l_preferred_rate_kw",
        "v2l_rectifier_efficiency",
        "v2l_charge_kw",
        "v2l_soc_tolerance_pct",
    }
)

# Subset of _OPTION_KEYS that hold secrets. These must never appear in cleartext
# in any response (CLAUDE.md top-priority rule): the API masks them before
# returning options. Kept here next to _OPTION_KEYS so the two stay in sync.
_SECRET_OPTION_KEYS = frozenset({"octopus_api_key", "axle_api_key", "agent_api_token"})


class ConfigError(RuntimeError):
    """Raised when the runtime configuration is invalid or incomplete."""


def _validation_error_reason(exc: ValidationError) -> str:
    """The failing field(s) + message per error; never an input value.

    ``str(ValidationError)`` embeds ``input_value`` for each error, which can
    be a secret option (``_SECRET_OPTION_KEYS``) or a nested ``devices``
    entry. Rejection reasons reach API clients, the agent surface and logs,
    so they are built from the error locations and messages only (#412).
    A location can also hold a submitted mapping key (``devices.0.entities.
    <key>``), so only known field names and list indexes are kept; any other
    part reads ``<key>`` (#457).
    """
    known = _known_field_names()
    reasons: list[str] = []
    for error in exc.errors():
        location = ".".join(
            str(part) if isinstance(part, int) or part in known else "<key>"
            for part in error["loc"]
        )
        reasons.append(f"{location}: {error['msg']}" if location else error["msg"])
    return "; ".join(reasons)


def _known_field_names() -> frozenset[str]:
    """Every field name of the config models, the only location parts a reason names."""
    models: tuple[type[BaseModel], ...] = (
        Settings, DeviceConfig, FixedTariffConfig, DynamicTariffConfig,
        OctopusIntelligentTariffConfig, AxleTariffConfig,
    )
    return frozenset(name for model in models for name in model.model_fields)


class FixedTariffConfig(BaseModel):
    """The ``fixed`` tariff provider's settings, validated at startup.

    Slice 1 keeps the existing flat rate/window config keys as the fixed
    provider's sole config source (no keys renamed, so no config break); this
    validates them up front so a bad tariff value is caught with a message
    naming the offending field rather than degrading a plan later. A later
    slice adds the provider selector these settings become nested under.
    """

    rate_offpeak: float = Field(ge=0)
    rate_peak: float = Field(ge=0)
    rate_export: float = Field(ge=0)
    window_start: str
    window_end: str

    @field_validator("window_start", "window_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            h, m = v.split(":")
            time(int(h), int(m))
        except (ValueError, TypeError) as exc:
            # No input value in the message: rejection reasons reach API clients
            # and logs, which must never echo an option's value (#412).
            raise ValueError("must be HH:MM") from exc
        return v


def validate_fixed_tariff(settings: Settings) -> None:
    """Validate the fixed provider's tariff settings; raise ConfigError on a bad field."""
    try:
        FixedTariffConfig(
            rate_offpeak=settings.rate_offpeak_gbp_kwh,
            rate_peak=settings.rate_peak_gbp_kwh,
            rate_export=settings.rate_export_gbp_kwh,
            window_start=settings.charge_window_start,
            window_end=settings.charge_window_end,
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid tariff configuration: {_validation_error_reason(exc)}") from exc


class DynamicTariffConfig(BaseModel):
    """The ``dynamic`` tariff provider's settings, validated at startup."""

    dynamic_rates_entity: str = Field(min_length=1)


def validate_dynamic_tariff(settings: Settings) -> None:
    """When `tariff_provider` is "dynamic", require a rates entity; else no-op."""
    if settings.tariff_provider != "dynamic":
        return
    try:
        DynamicTariffConfig(dynamic_rates_entity=settings.dynamic_rates_entity)
    except ValidationError as exc:
        raise ConfigError(f"Invalid tariff configuration: {_validation_error_reason(exc)}") from exc


class OctopusIntelligentTariffConfig(BaseModel):
    """The ``octopus_intelligent`` tariff provider's settings, validated at startup."""

    octopus_api_key: str = Field(min_length=1)
    octopus_account_number: str = Field(min_length=1)
    octopus_product_code: str = Field(min_length=1)
    octopus_tariff_code: str = Field(min_length=1)


def validate_octopus_intelligent_tariff(settings: Settings) -> None:
    """Validate Octopus config, allowing missing rate codes when dispatch auth is set."""
    if settings.tariff_provider != "octopus_intelligent":
        return
    # With dispatch authentication configured, missing product/tariff codes
    # are discovered from the account (#273). If discovery fails, the fetch
    # reports an OctopusApiError and the provider uses its fixed schedule.
    if (
        settings.octopus_api_key
        and settings.octopus_account_number
        and (not settings.octopus_product_code or not settings.octopus_tariff_code)
    ):
        return
    try:
        OctopusIntelligentTariffConfig(
            octopus_api_key=settings.octopus_api_key,
            octopus_account_number=settings.octopus_account_number,
            octopus_product_code=settings.octopus_product_code,
            octopus_tariff_code=settings.octopus_tariff_code,
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid tariff configuration: {_validation_error_reason(exc)}") from exc


class AxleTariffConfig(BaseModel):
    """The supervised Axle event provider's settings."""

    axle_api_key: str = ""
    axle_event_entity: str = ""
    axle_event_rate_gbp_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def _source_configured(self) -> AxleTariffConfig:
        if not self.axle_api_key and not self.axle_event_entity:
            raise ValueError("set axle_api_key or axle_event_entity")
        return self


def validate_axle_tariff(settings: Settings) -> None:
    """When ``export_event_provider`` is ``axle``, require one event source."""
    if settings.export_event_provider != "axle":
        return
    try:
        AxleTariffConfig(
            axle_api_key=settings.axle_api_key,
            axle_event_entity=settings.axle_event_entity,
            axle_event_rate_gbp_kwh=settings.axle_event_rate_gbp_kwh,
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid tariff configuration: {_validation_error_reason(exc)}") from exc


class DeviceConfig(BaseModel):
    """One controllable device. Phase 7 ships type == "inverter" only."""

    id: str
    type: Literal["inverter"] = "inverter"
    driver: str
    entities: dict[str, str] = Field(default_factory=dict)


class FieldMeta(TypedDict):
    """Eventual owner, secrecy and activation policy for one runtime field."""

    owner: Literal["addon", "integration"]
    secret: bool
    reload: Literal["live", "restart"]


def _metadata(
    owner: Literal["addon", "integration"],
    reload: Literal["live", "restart"] = "live",
    *,
    secret: bool = False,
) -> Any:
    return Field(json_schema_extra={"owner": owner, "secret": secret, "reload": reload})


class Settings(BaseSettings):
    """Top-level configuration for ha-spark."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Filter before decoding: even malformed runtime JSON in the environment
        # must have no effect. File secrets are not a configuration source.
        env = cast(EnvSettingsSource, env_settings)
        dotenv = cast(EnvSettingsSource, dotenv_settings)
        effective = {**dotenv.env_vars, **env.env_vars, **init_settings()}
        if effective.get("ha_url") and effective.get("ha_token"):
            allowed = {"ha_url", "ha_token", "db_path", "log_level"}
        else:
            allowed = {"supervisor_token", "log_level"}
        for source in (env, dotenv):
            source.env_vars = {
                key: value for key, value in source.env_vars.items() if key in allowed
            }
        return init_settings, env_settings, dotenv_settings

    # --- Home Assistant ---
    # Add-on mode is the default (URLs/token derived from the Supervisor).
    # Setting both ha_url and ha_token switches to standalone/dev mode.
    ha_url: str = Field(default="")
    ha_token: str = Field(default="")
    supervisor_token: str = Field(default="")
    ha_timeout: Annotated[float, _metadata("addon")] = Field(default=10.0)

    # --- Ollama (single remote tier, typically reached over Tailscale) ---
    ollama_url: Annotated[str, _metadata("addon")] = Field(default="http://127.0.0.1:11434")
    ollama_model: Annotated[str, _metadata("addon")] = Field(default="qwen3:14b")
    # Context window; Ollama's default of 2048 truncates agent prompts.
    ollama_num_ctx: Annotated[int, _metadata("addon")] = Field(default=32768)
    ollama_timeout: Annotated[float, _metadata("addon")] = Field(default=120.0)
    ollama_health_timeout: Annotated[float, _metadata("addon")] = Field(default=2.0)

    # --- Storage ---
    db_path: str = Field(default="data/ha_spark.db")

    # --- Logging ---
    log_level: str = Field(default="INFO")

    # --- Energy planner ---
    # Battery / inverter model.
    battery_capacity_kwh: Annotated[float, _metadata("integration")] = Field(default=26.88)
    # Fallback if the sensor is unavailable.
    battery_voltage_v: Annotated[float, _metadata("integration")] = Field(default=51.0)
    min_soc: Annotated[float, _metadata("addon")] = Field(default=20.0)
    target_soc_cap: Annotated[float, _metadata("addon")] = Field(default=90.0)
    max_charge_current_a: Annotated[float, _metadata("integration")] = Field(default=62.5)
    # Optional Solis-only current ceiling used after sustained SoC failures.
    # None disables fallback programming; the driver also caps it at the
    # normal configured current and the 62.5 A register limit.
    solis_fallback_current_a: Annotated[float | None, _metadata("integration")] = Field(
        default=None, ge=1, le=62.5
    )
    # Conservative planning ceiling for battery discharge.  It is distinct
    # from Solis's fixed hardware command and keeps the planner inverter-agnostic.
    battery_discharge_ceiling_kw: Annotated[float, _metadata("integration")] = Field(
        default=3.2, ge=0
    )
    # Installation-specific grid-export cap. Defaults to the G98 fit-and-inform
    # limit (16 A single-phase); raise it only to a G99-approved figure.
    dno_export_limit_kw: Annotated[float, _metadata("integration")] = Field(default=3.68, ge=0)
    # Safety margin applied to the forecast deficit before sizing the charge.
    charge_buffer_pct: Annotated[float, _metadata("addon")] = Field(default=20.0)
    # Round-trip AC->DC->AC efficiency: the planner buys required/efficiency.
    charge_efficiency: Annotated[float, _metadata("addon")] = Field(default=0.90)
    # Overnight sizing: "deficit" buys only the forecast shortfall;
    # "fill" charges to target_soc_cap every night (wins once export > off-peak).
    charge_strategy: Annotated[Literal["deficit", "fill"], _metadata("addon")] = Field(
        default="deficit"
    )

    # Forecast / model coefficients.
    solar_haircut_k: Annotated[float, _metadata("addon")] = Field(default=1.0)
    # Solcast percentile to plan on: 50 (median), 10 (conservative), 90 (optimistic).
    solar_percentile: Annotated[Literal[10, 50, 90], _metadata("addon")] = Field(default=50)
    forecast_days: Annotated[int, _metadata("addon")] = Field(default=14)
    # Fallback when statistics are unavailable.
    expected_load_kwh: Annotated[float, _metadata("addon")] = Field(default=24.0)

    # Fixed cheap charge window (local HH:MM).
    charge_window_start: Annotated[str, _metadata("integration")] = Field(default="23:30")
    charge_window_end: Annotated[str, _metadata("integration")] = Field(default="05:30")

    # Two-rate tariff (GBP/kWh): off-peak inside the window/dispatch slots, else peak.
    rate_offpeak_gbp_kwh: Annotated[float, _metadata("addon")] = Field(default=0.069)
    rate_peak_gbp_kwh: Annotated[float, _metadata("addon")] = Field(default=0.30)
    # Export/feed-in rate (GBP/kWh); 0 disables export revenue in cost projections.
    rate_export_gbp_kwh: Annotated[float, _metadata("addon")] = Field(default=0.0)

    # Tariff provider: "fixed" costs against rate_offpeak/rate_peak + the charge
    # window above; "dynamic" costs each slot at its live price from an HA
    # half-hourly price sensor (falls back to fixed on a missing/bad read).
    tariff_provider: Annotated[
        Literal["fixed", "dynamic", "octopus_intelligent"], _metadata("addon")
    ] = Field(default="fixed")
    export_event_provider: Annotated[Literal["none", "axle"], _metadata("addon")] = "none"
    dynamic_rates_entity: Annotated[str, _metadata("addon")] = Field(default="")
    # Optional: a second entity for tomorrow's rates (many integrations publish
    # today/tomorrow as separate entities). Blank is fine — slots past today's
    # coverage just fall back to the fixed rate.
    dynamic_rates_entity_tomorrow: Annotated[str, _metadata("addon")] = Field(default="")

    # v2 slot-profile load model (from imported Octopus half-hourly consumption).
    profile_min_days: Annotated[int, _metadata("addon")] = Field(default=7)
    profile_history_days: Annotated[int, _metadata("addon")] = Field(default=60)
    timezone: Annotated[str, _metadata("integration")] = Field(default="Europe/London")

    # Retained for configuration compatibility; the daemon now replans every
    # local half-hour slot.
    plan_run_time: Annotated[str, _metadata("addon")] = Field(default="22:00")

    # Statistic whose history seeds `ha-spark backfill-load` (a true-load power
    # or energy sensor); the CLI's --from flag overrides it.
    backfill_source_entity: Annotated[str, _metadata("addon")] = Field(default="")

    # Derived base load (ADR-0001): per-component HA statistic IDs.
    # `derive_grid_import_entity` is required when any are set; the others
    # contribute zero with a degradation note when unset. The invert flags
    # are explicit (never inferred): true flips the canonical sign after the
    # usual unit conversion, so a mis-signed sensor never silently corrupts
    # the balance.
    derive_grid_import_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_grid_export_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_solar_generation_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_battery_charge_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_battery_discharge_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_ev_charge_entity: Annotated[str, _metadata("addon")] = Field(default="")
    derive_invert_grid_import: Annotated[bool, _metadata("addon")] = Field(default=False)
    derive_invert_grid_export: Annotated[bool, _metadata("addon")] = Field(default=False)
    derive_invert_solar_generation: Annotated[bool, _metadata("addon")] = Field(default=False)
    derive_invert_battery_charge: Annotated[bool, _metadata("addon")] = Field(default=False)
    derive_invert_battery_discharge: Annotated[bool, _metadata("addon")] = Field(default=False)
    derive_invert_ev_charge: Annotated[bool, _metadata("addon")] = Field(default=False)

    # Octopus REST API (for `pull-consumption`; CSV import needs none of these).
    # `octopus_api_key` also drives the `octopus_intelligent` tariff provider
    # below (Kraken GraphQL auth + REST standard-unit-rates).
    octopus_api_key: Annotated[str, _metadata("addon", secret=True)] = Field(default="")
    octopus_mpan: Annotated[str, _metadata("addon")] = Field(default="")
    octopus_meter_serial: Annotated[str, _metadata("addon")] = Field(default="")
    octopus_api_url: Annotated[str, _metadata("addon")] = Field(
        default="https://api.octopus.energy/v1"
    )
    # Octopus Intelligent tariff provider: account number for the GraphQL
    # plannedDispatches query, product/tariff code for the standard-unit-rates
    # REST endpoint (e.g. "INTELLI-VAR-22-10-14" / "E-1R-INTELLI-VAR-22-10-14-A").
    octopus_account_number: Annotated[str, _metadata("addon")] = Field(default="")
    octopus_product_code: Annotated[str, _metadata("addon")] = Field(default="")
    octopus_tariff_code: Annotated[str, _metadata("addon")] = Field(default="")

    # Axle supervised export-event source. The API key is the static token from
    # Axle's Home Assistant account page; the entity is the optional HA mirror.
    axle_api_key: Annotated[str, _metadata("addon", secret=True)] = Field(default="")
    axle_api_url: Annotated[str, _metadata("addon")] = Field(default="https://api.axle.energy")
    axle_event_entity: Annotated[str, _metadata("addon")] = Field(default="")
    axle_event_rate_gbp_kwh: Annotated[float, _metadata("addon")] = Field(default=1.0, ge=0)
    # Shared HA notify.<service> target for all notices; V2L retains a deprecated fallback.
    notify_service: Annotated[str, _metadata("addon")] = Field(default="")

    # HA entity IDs (all overridable). Blank by default; set via `ha-spark
    # onboard` (entity auto-discovery) or the `solis` preset (ha_spark/presets.py),
    # which holds the values for the original Solis/Solcast/Octopus/zappi setup.
    soc_entity: Annotated[str, _metadata("integration")] = Field(default="")
    # How old Home Assistant's top-level `last_reported` for the SoC sensor may
    # be before the measurement is judged stale (ha_spark/energy/soc_integrity.py).
    # Untrusted SoC blocks real charge writes, so this is a safety threshold.
    soc_max_report_age_minutes: Annotated[float, _metadata("addon")] = Field(default=10.0, gt=0)
    # Consecutive failed SoC observations before fallback entry (#115). The first
    # failure already blocks SoC-based programming and charge-rate increases.
    soc_failure_threshold: Annotated[int, _metadata("addon")] = Field(default=3, ge=1)
    # Continuous passing minutes before an active Solis fallback recovers (#117).
    # Observed at the one-minute cadence; any failure restarts the count.
    soc_recovery_minutes: Annotated[int, _metadata("addon")] = Field(default=10, ge=1)
    battery_voltage_entity: Annotated[str, _metadata("integration")] = Field(default="")
    battery_power_entity: Annotated[str, _metadata("integration")] = Field(default="")
    solar_tomorrow_entity: Annotated[str, _metadata("addon")] = Field(default="")
    # Optional Solcast "forecast today" sensor (#398): today's solar for
    # charge sizing while the charge window's day is today. Without it (or on
    # an invalid payload) today's solar counts as zero, which oversizes the
    # charge and never undersizes it.
    solar_today_entity: Annotated[str, _metadata("addon")] = Field(default="")
    octopus_rate_entity: Annotated[str, _metadata("integration")] = Field(default="")
    dispatch_entity: Annotated[str, _metadata("integration")] = Field(default="")
    ev_plug_entity: Annotated[str, _metadata("integration")] = Field(default="")
    ev_status_entity: Annotated[str, _metadata("integration")] = Field(default="")
    # Direct car draw, W or kW.
    ev_power_entity: Annotated[str, _metadata("integration")] = Field(default="")
    # Independent import evidence.
    dispatch_grid_power_entity: Annotated[str, _metadata("integration")] = Field(default="")
    dispatch_grid_power_invert: Annotated[bool, _metadata("integration")] = Field(default=False)

    # Freshness metadata only; hold consumers are deliberately unchanged (#419).
    hold_degraded_minutes: Annotated[int, _metadata("addon")] = Field(default=60, ge=5, le=1440)
    readiness_window_days: Annotated[int, _metadata("addon")] = Field(default=21, gt=0)
    readiness_mae_improvement_pct: Annotated[float, _metadata("addon")] = Field(
        default=10, ge=0, le=100
    )
    readiness_max_cost_delta: Annotated[float, _metadata("addon")] = Field(
        default=0.0, allow_inf_nan=False
    )
    readiness_min_guard_coverage_pct: Annotated[float, _metadata("addon")] = Field(
        default=90, ge=0, le=100
    )
    ev_status_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    ev_plug_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    ev_power_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    live_dispatch_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    adjusted_rate_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    dispatch_grid_power_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    battery_power_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=300, gt=0, le=86400
    )
    octopus_dispatch_max_age_seconds: Annotated[float | None, _metadata("addon")] = Field(
        default=2700, gt=0, le=86400
    )
    ev_liveness_entity: Annotated[str, _metadata("integration")] = ""
    dispatch_liveness_entity: Annotated[str, _metadata("integration")] = ""
    battery_power_discharge_sign: Annotated[
        Literal["positive", "negative"] | None, _metadata("integration")
    ] = None
    # True house load excluding battery/EV.
    consumption_energy_entity: Annotated[str, _metadata("addon")] = Field(default="")
    # Live supply guard: throttle battery charging while whole-house AC draw
    # exceeds supply_max_current_a. Disabled while grid_power_entity is empty.
    # W; whole-house grid/supply draw.
    grid_power_entity: Annotated[str, _metadata("integration")] = Field(default="")
    supply_max_current_a: Annotated[float, _metadata("integration")] = Field(default=75.0)
    # AC volts, converts W -> A.
    supply_voltage_v: Annotated[float, _metadata("integration")] = Field(default=240.0)
    charge_current_entity: Annotated[str, _metadata("integration")] = Field(default="")
    inverter_power_switch_entity: Annotated[str, _metadata("integration")] = Field(default="")
    ha_template_charge_needed_entity: Annotated[str, _metadata("addon")] = Field(default="")

    # Inverter selector: picks the Charger adapter (ha_spark/energy/chargers.py).
    inverter: Annotated[Literal["solis", "alphaess"], _metadata("integration", "restart")] = Field(
        default="solis"
    )
    # Solis native control: the thin HA `modbus:` overlay hub (#90) ha-spark
    # writes the timed-slot registers through (`modbus.write_register`) and reads
    # back via its `sensor.<hub>_*` entities. The window/current/work-mode
    # registers are fixed in the driver (docs/solis-control-modbus-overlay.yaml).
    solis_model_confirmed: Annotated[bool, _metadata("integration")] = False
    solis_control_hub: Annotated[str, _metadata("integration", "restart")] = Field(
        default="solis_control"
    )
    solis_modbus_slave: Annotated[int, _metadata("integration", "restart")] = Field(default=1)
    # How far the inverter clock may disagree with the household clock before
    # an export window is refused and `health` warns (#155/#161).
    # Below health's 30-minute failure threshold, so a failure is always refused.
    inverter_clock_tolerance_minutes: Annotated[float, _metadata("integration")] = Field(
        default=5.0, ge=0.1, lt=30
    )
    # Opt-in: sync the inverter clock at each daylight-saving change (#161).
    inverter_clock_dst_sync: Annotated[bool, _metadata("integration")] = Field(default=False)
    # AlphaESS system serial for the alphaess.setbatterycharge service call.
    alphaess_serial: Annotated[str, _metadata("integration", "restart")] = Field(default="")

    # Structured device config (Phase 7): a list of controllable devices. Left
    # empty in flat-config installs; the after-validator synthesizes one inverter
    # device from the flat entity keys below so existing setups need no change.
    devices: Annotated[list[DeviceConfig], _metadata("integration", "restart")] = Field(
        default_factory=list
    )

    # Forecast ledger signal sampling (Phase 6A): recorded so training data
    # accumulates ahead of the models (6B+) that will consume it.
    # Comma-separated person/device_tracker entity ids; empty disables occupancy sampling.
    person_entities: Annotated[str, _metadata("addon")] = Field(default="")
    # Dedicated heat-pump energy sensor (kWh); empty disables heatpump_kwh sampling.
    heatpump_energy_entity: Annotated[str, _metadata("addon")] = Field(default="")
    # Weather entity with a `temperature` attribute (e.g. HA's built-in Met.no `weather.home`).
    outdoor_weather_entity: Annotated[str, _metadata("addon")] = Field(default="weather.home")

    # Weather-aware ML load model (Phase 6B; needs the [habits] extra).
    # "median" = slot-profile only; "ml" = always prefer the ML model when it can
    # run; "auto" = use ML only once forecast-eval shows it beating the median
    # over the trailing 14 days (safe default: degrades to median until then).
    load_model: Annotated[Literal["median", "ml", "auto"], _metadata("addon")] = Field(
        default="auto"
    )
    # "quantile" replaces the fixed charge_buffer_pct with (P90-P50)/P50 from the
    # ML model whenever an ML forecast drives the plan.
    buffer_mode: Annotated[Literal["fixed", "quantile"], _metadata("addon")] = Field(
        default="fixed"
    )
    # Site coordinates for Open-Meteo; unset -> read from HA /api/config.
    latitude: Annotated[float | None, _metadata("addon")] = Field(default=None)
    longitude: Annotated[float | None, _metadata("addon")] = Field(default=None)

    # Context store (Phase 6C): load multipliers for an active dated fact.
    # away (holiday) lightens the forecast; guests heightens it. high_usage/
    # low_usage facts carry their own factor instead.
    away_load_factor: Annotated[float, _metadata("addon")] = Field(default=0.4)
    guests_load_factor: Annotated[float, _metadata("addon")] = Field(default=1.3)

    # Agent surface (MCP + OpenAPI): "off" disables the surface entirely.
    agent_surface: Annotated[Literal["off", "on"], _metadata("addon", "restart")] = Field(
        default="off"
    )
    # Exposure level gates which tools are registered: read-only, read + act
    # (reviewable proposals), or read + write (direct actuation).
    agent_exposure: Annotated[Literal["read", "read_act", "read_write"], _metadata("addon")] = (
        Field(default="read_act")
    )
    # Bearer token for the agent surface; blank means auto-generate at runtime.
    agent_api_token: Annotated[str, _metadata("addon", "restart", secret=True)] = Field(default="")
    # Expose the agent surface port directly (bypassing ingress); off by default.
    agent_expose_port: Annotated[bool, _metadata("addon", "restart")] = Field(default=False)

    # --- V2L (Vehicle-to-Load) observe + tally + notify ---
    # The car's V2L discharge-power sensor (W). Empty disables the feature.
    v2l_power_entity: Annotated[str, _metadata("integration")] = Field(default="")
    # Round-trip AC->DC->AC->DC efficiency, used to discount the cost of
    # refilling the car later. One calibration knob folding both DC<->AC stages.
    v2l_round_trip_efficiency: Annotated[float, _metadata("addon")] = Field(default=0.85)
    # GBP/kWh import rate being offset now (peak) vs the cheap rate to refill.
    v2l_peak_rate_gbp: Annotated[float, _metadata("addon")] = Field(default=0.30)
    v2l_offpeak_rate_gbp: Annotated[float, _metadata("addon")] = Field(default=0.07)
    # Local time (HH:MM) the cheap window starts; the unplug nudge fires here.
    v2l_cutoff_time: Annotated[str, _metadata("addon")] = Field(default="01:00")
    # HA notify.<service> target (e.g. mobile_app_x). Empty disables notifications.
    v2l_notify_service: Annotated[str, _metadata("addon")] = Field(default="")
    # Optional V2L budget (kWh) standing in for car SoC; 0 disables the
    # predictive plug-in warning.
    v2l_budget_kwh: Annotated[float, _metadata("addon")] = Field(default=0.0)
    v2l_max_rate_kw: Annotated[float, _metadata("integration")] = Field(
        default=3.0, gt=0, allow_inf_nan=False
    )
    v2l_preferred_rate_kw: Annotated[float, _metadata("addon")] = Field(
        default=1.5, gt=0, allow_inf_nan=False
    )
    # Rectifier AC->DC efficiency from the car into the house battery, and the
    # DC rate it charges at (measured: 10 A AC input limit) (#208).
    v2l_rectifier_efficiency: Annotated[float, _metadata("addon")] = Field(default=0.94, gt=0, le=1)
    v2l_charge_kw: Annotated[float, _metadata("integration")] = Field(default=2.15, gt=0)
    # Points below the overnight plan's expected SoC on an Axle event day that
    # count as a miscalculation worth a V2L top-up request (#208).
    v2l_soc_tolerance_pct: Annotated[float, _metadata("addon")] = Field(default=5.0, ge=0)

    @field_validator("notify_service", "v2l_notify_service")
    @classmethod
    def _normalise_notify_service(cls, value: str) -> str:
        return value.removeprefix("notify.")

    @field_validator("solar_percentile", mode="before")
    @classmethod
    def _coerce_solar_percentile(cls, v: object) -> object:
        # The add-on schema `list(10|50|90)` delivers the choice as a string in
        # /data/options.json; the field is an int Literal.
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
        return v

    @model_validator(mode="after")
    def _validate_v2l_rates(self) -> Settings:
        if self.v2l_preferred_rate_kw > self.v2l_max_rate_kw:
            raise ValueError("v2l_preferred_rate_kw must not exceed v2l_max_rate_kw")
        return self

    @model_validator(mode="after")
    def _synthesize_devices(self) -> Settings:
        """Dual-read shim: if no structured ``devices``, build one inverter device
        from the flat entity keys in memory. Idempotent; never rewrites options.json."""
        if not self.devices:
            self.devices = [
                DeviceConfig(
                    id="main_inverter",
                    type="inverter",
                    driver=self.inverter,
                    entities={
                        # charge_current is telemetry/dashboard only for Solis
                        # (control is native modbus via solis_control_hub); kept
                        # generic here for the dashboard row and other drivers.
                        "charge_current": self.charge_current_entity,
                        "power_switch": self.inverter_power_switch_entity,
                    },
                )
            ]
        return self

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


def field_metadata() -> dict[str, FieldMeta]:
    """Runtime-field vocabulary and classification; bootstrap fields are excluded."""
    return {
        name: cast(FieldMeta, dict(field.json_schema_extra))
        for name, field in Settings.model_fields.items()
        if isinstance(field.json_schema_extra, dict) and "owner" in field.json_schema_extra
    }


def _validated_settings(settings: Settings) -> Settings:
    """Run the cross-field tariff validations; raise ``ConfigError`` naming the field."""
    validate_fixed_tariff(settings)
    validate_dynamic_tariff(settings)
    validate_octopus_intelligent_tariff(settings)
    validate_axle_tariff(settings)
    return settings


def actuation_settings_changed(previous: Settings, current: Settings) -> bool:
    """Whether accepted settings fence work planned under the previous mapping/limits."""
    fields = {
        "devices", "charge_window_start", "charge_window_end", "timezone",
        "solis_control_hub", "solis_modbus_slave", "solis_fallback_current_a",
        "max_charge_current_a", "battery_voltage_v", "battery_discharge_ceiling_kw",
        "dno_export_limit_kw", "supply_max_current_a", "supply_voltage_v",
        "min_soc", "target_soc_cap", "alphaess_serial", "ha_url",
    }
    fields.update(name for name in Settings.model_fields if name.endswith("_entity"))
    return any(getattr(previous, name) != getattr(current, name) for name in fields)


def validate_options_document(options: dict[str, Any]) -> Settings:
    """Build complete :class:`Settings` from a candidate options document and validate it.

    The same overlay semantics as :func:`load_settings` (only runtime fields
    honoured, ``None`` values dropped) and the same validation, minus the
    environment credential check -- credentials come from the environment,
    not the options document. Used to validate the complete merged candidate
    *before* it is persisted by an options save (API, agent, MCP), so an
    invalid document is rejected without touching disk or the running
    settings. Raises ``ConfigError`` naming the failing field(s); the reason
    never carries an input value (see ``_validation_error_reason``).
    """
    overlay = {k: v for k, v in options.items() if k in field_metadata() and v is not None}
    try:
        settings = Settings(**overlay)
    except ValidationError as exc:
        raise ConfigError(_validation_error_reason(exc)) from exc
    return _validated_settings(settings)


def load_settings(*, validate: bool = True) -> Settings:
    """Read configuration without writing; only the daemon initialises the store.

    ``validate=False`` skips credential and cross-field tariff checks for
    diagnostics. Invalid persisted field values always raise a safe ConfigError.
    """
    from ha_spark.settings_service import SettingsService

    return SettingsService().load(validate=validate)
