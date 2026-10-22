import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ha_spark.config import (
    _OPTION_KEYS,
    _SECRET_OPTION_KEYS,
    SUPERVISOR_REST_URL,
    SUPERVISOR_WS_URL,
    ConfigError,
    DeviceConfig,
    Settings,
    field_metadata,
    load_settings,
)
from ha_spark.settings_service import _read_options_overlay

ADDON_CONFIG = Path(__file__).parent.parent / "ha_spark_addon" / "config.yaml"


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests hermetic: ignore any developer .env and stray env vars."""
    monkeypatch.chdir(tmp_path)
    for var in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN", "OLLAMA_URL", "OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_addon_mode_uses_supervisor_endpoints() -> None:
    s = Settings(supervisor_token="sup-token")
    assert s.is_standalone is False
    assert s.ha_rest_url == SUPERVISOR_REST_URL
    assert s.ha_websocket_url == SUPERVISOR_WS_URL
    assert s.auth_token == "sup-token"


def test_standalone_requires_both_url_and_token() -> None:
    # URL alone does not switch out of add-on mode.
    assert Settings(ha_url="http://ha.local:8123").is_standalone is False
    assert Settings(ha_url="http://ha.local:8123").ha_rest_url == SUPERVISOR_REST_URL


def test_standalone_url_derivation_http() -> None:
    s = Settings(ha_url="http://homeassistant.local:8123/", ha_token="t")
    assert s.is_standalone is True
    assert s.ha_websocket_url == "ws://homeassistant.local:8123/api/websocket"
    assert s.ha_rest_url == "http://homeassistant.local:8123/api"


def test_standalone_url_derivation_https() -> None:
    s = Settings(ha_url="https://ha.example.com", ha_token="t")
    assert s.ha_websocket_url == "wss://ha.example.com/api/websocket"


def test_auth_token_prefers_dev_token() -> None:
    assert Settings(ha_token="dev", supervisor_token="sup").auth_token == "dev"
    assert Settings(supervisor_token="sup").auth_token == "sup"


def test_single_ollama_tier_defaults() -> None:
    s = Settings()
    assert s.ollama_url
    assert s.ollama_model == "qwen3:14b"
    assert s.ollama_num_ctx == 32768
    assert s.db_path.endswith(".db")


def test_load_settings_fails_fast_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # No /data/options.json overlay in the test environment.
    monkeypatch.setattr("ha_spark.settings_service._read_options_overlay", lambda path: {})
    with pytest.raises(ConfigError):
        load_settings()


def test_load_settings_ok_in_addon_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    monkeypatch.setattr("ha_spark.settings_service._read_options_overlay", lambda path: {})
    settings = load_settings()
    assert settings.is_standalone is False
    assert settings.auth_token == "sup-token"


def test_load_settings_validate_false_skips_credential_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Diagnostic paths (`ha-spark health`) must build settings without creds
    # while still honouring the options overlay.
    monkeypatch.setattr(
        "ha_spark.settings_service._read_options_overlay",
        lambda path: {"ollama_url": "http://100.1.2.3:11434"},
    )
    settings = load_settings(validate=False)
    assert settings.ollama_url == "http://100.1.2.3:11434"


def test_options_overlay_accepts_entity_ids(tmp_path: Path) -> None:
    options = tmp_path / "options.json"
    options.write_text(
        json.dumps(
            {
                "soc_entity": "sensor.my_soc",
                "charge_current_entity": "number.my_charge_current",
                "not_an_option": "ignored",
            }
        ),
        encoding="utf-8",
    )
    overlay = _read_options_overlay(options)
    assert overlay == {
        "soc_entity": "sensor.my_soc",
        "charge_current_entity": "number.my_charge_current",
    }
    s = Settings(**overlay)
    assert s.soc_entity == "sensor.my_soc"
    assert s.charge_current_entity == "number.my_charge_current"


def test_solar_percentile_coerces_addon_string() -> None:
    # The add-on schema `list(10|50|90)` delivers the choice as a string.
    assert Settings(solar_percentile="10").solar_percentile == 10  # type: ignore[arg-type]
    assert Settings(solar_percentile=90).solar_percentile == 90


def test_addon_bootstrap_schema_and_runtime_field_classification() -> None:
    import yaml

    addon = yaml.safe_load(ADDON_CONFIG.read_text(encoding="utf-8"))
    assert set(addon["schema"]) == set(addon["options"]) == {"log_level"}
    metadata = field_metadata()
    bootstrap = {"ha_url", "ha_token", "supervisor_token", "db_path", "log_level"}
    assert set(metadata) == Settings.model_fields.keys() - bootstrap
    for name, meta in metadata.items():
        assert meta["owner"] in {"addon", "integration"}, name
        assert type(meta["secret"]) is bool, name
        assert meta["reload"] in {"live", "restart"}, name
    assert {name for name, meta in metadata.items() if meta["secret"]} == _SECRET_OPTION_KEYS
    for name in bootstrap:
        assert Settings.model_fields[name].json_schema_extra is None
    legacy = yaml.safe_load((Path(__file__).parent / "fixtures/addon-config-0.24.yaml").read_text())
    assert set(legacy["options"]) <= _OPTION_KEYS
    assert set(legacy["schema"]) <= _OPTION_KEYS


def test_restart_fields_match_startup_bindings() -> None:
    metadata = field_metadata()
    assert {name for name, meta in metadata.items() if meta["reload"] == "restart"} == {
        "inverter",
        "devices",
        "solis_control_hub",
        "solis_modbus_slave",
        "alphaess_serial",
        "agent_surface",
        "agent_expose_port",
        "agent_api_token",
    }
    for name in ("timezone", "ha_timeout", "ollama_url", "inverter_power_switch_entity"):
        assert metadata[name]["reload"] == "live"


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("source", ["env", "dotenv"])
def test_runtime_environment_is_ignored(monkeypatch, tmp_path, standalone, source):
    values = {
        "MIN_SOC": "30",
        "OCTOPUS_API_KEY": "env-key",
        "TARIFF_PROVIDER": "invalid",
        "DEVICES": "not-json",
        "DB_PATH": "custom/ha_spark.db",
        "SUPERVISOR_TOKEN": "sup-token",
        "LOG_LEVEL": "DEBUG",
    }
    if standalone:
        values.update(HA_URL="http://ha.test", HA_TOKEN="test")
    if source == "env":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    else:
        (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()))
        monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    settings = Settings()
    assert settings.is_standalone is standalone
    assert settings.min_soc == 20
    assert settings.octopus_api_key == ""
    assert settings.tariff_provider == "fixed"
    assert settings.log_level == "DEBUG"
    assert settings.db_path == ("custom/ha_spark.db" if standalone else "data/ha_spark.db")
    assert settings.supervisor_token == ("" if standalone else "sup-token")
    assert Settings(min_soc=25).min_soc == 25


def test_agent_defaults() -> None:
    s = Settings(ha_url="http://ha.test", ha_token="x")  # type: ignore[call-arg]
    assert s.agent_surface == "off"
    assert s.agent_exposure == "read_act"
    assert s.agent_expose_port is False
    assert s.agent_api_token == ""


def test_agent_options_in_whitelist() -> None:
    for key in ("agent_surface", "agent_exposure", "agent_api_token", "agent_expose_port"):
        assert key in _OPTION_KEYS


def test_v2l_options_load_from_overlay() -> None:
    s = Settings(
        v2l_power_entity="sensor.car_v2l_power",
        v2l_round_trip_efficiency=0.8,
        v2l_peak_rate_gbp=0.32,
        v2l_offpeak_rate_gbp=0.07,
        v2l_cutoff_time="01:00",
        v2l_notify_service="mobile_app_phone",
        v2l_budget_kwh=5.0,
    )
    assert s.v2l_power_entity == "sensor.car_v2l_power"
    assert s.v2l_round_trip_efficiency == 0.8
    assert s.v2l_budget_kwh == 5.0
    # defaults
    defaults = Settings(_env_file=None)
    assert defaults.v2l_power_entity == ""
    assert defaults.v2l_round_trip_efficiency == 0.85
    assert defaults.v2l_cutoff_time == "01:00"


@pytest.mark.parametrize("field", ["notify_service", "v2l_notify_service"])
@pytest.mark.parametrize("value", ["notify.mobile_app_x", "mobile_app_x"])
def test_notify_service_loads_as_bare_name(field: str, value: str) -> None:
    settings = Settings(**{field: value})
    assert getattr(settings, field) == "mobile_app_x"


def test_export_planner_limits_are_configurable_across_addon_surfaces() -> None:
    settings = Settings(battery_discharge_ceiling_kw=3.2, dno_export_limit_kw=7.36)

    assert settings.battery_discharge_ceiling_kw == 3.2
    assert settings.dno_export_limit_kw == 7.36
    assert "battery_discharge_ceiling_kw" in _OPTION_KEYS
    assert "dno_export_limit_kw" in _OPTION_KEYS


def test_dno_export_limit_defaults_to_g98_minimum_on_every_surface() -> None:
    """3.68 kW (16 A single-phase) is the G98 fit-and-inform limit; more needs G99."""
    from datetime import time

    from ha_spark.energy.models import PlannerConfig

    planner = PlannerConfig(
        capacity_kwh=26.88,
        voltage_v=51.0,
        min_soc=20.0,
        target_cap=90.0,
        max_current_a=62.5,
        solar_haircut_k=1.0,
        window_start=time(23, 30),
        window_end=time(5, 30),
    )
    assert Settings().dno_export_limit_kw == 3.68
    assert planner.dno_export_limit_kw == 3.68


def test_inverter_clock_tolerance_defaults_to_five_minutes() -> None:
    assert Settings().inverter_clock_tolerance_minutes == 5.0
    assert "inverter_clock_tolerance_minutes" in _OPTION_KEYS


def test_inverter_clock_tolerance_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(inverter_clock_tolerance_minutes=0)


def test_devices_synthesized_from_flat_keys_when_absent() -> None:
    s = Settings(
        supervisor_token="sup",
        inverter="solis",
        charge_current_entity="number.cc",
        inverter_power_switch_entity="select.pw",
    )  # type: ignore[call-arg]
    assert len(s.devices) == 1
    d = s.devices[0]
    assert isinstance(d, DeviceConfig)
    assert d.id == "main_inverter"
    assert d.type == "inverter"
    assert d.driver == "solis"
    assert "control" not in d.model_dump()
    assert d.entities["charge_current"] == "number.cc"
    assert d.entities["power_switch"] == "select.pw"


def test_explicit_devices_list_parses_through() -> None:
    s = Settings(
        supervisor_token="sup",
        devices=[
            {
                "id": "main_inverter",
                "type": "inverter",
                "driver": "alphaess",
                "control": "observe",
                "entities": {"charge_current": "number.x"},
            }
        ],
    )  # type: ignore[call-arg]
    assert len(s.devices) == 1
    assert s.devices[0].driver == "alphaess"
    assert "control" not in s.devices[0].model_dump()


def test_devices_in_option_keys() -> None:
    assert "devices" in _OPTION_KEYS


def test_soc_max_report_age_defaults_to_ten_minutes() -> None:
    assert Settings(ha_url="http://x", ha_token="t").soc_max_report_age_minutes == 10.0


def test_soc_max_report_age_is_configurable_across_surfaces() -> None:
    assert "soc_max_report_age_minutes" in _OPTION_KEYS
    assert (
        Settings(
            ha_url="http://x", ha_token="t", soc_max_report_age_minutes=2.5
        ).soc_max_report_age_minutes
        == 2.5
    )


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_soc_max_report_age_must_be_positive(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(ha_url="http://x", ha_token="t", soc_max_report_age_minutes=bad)


def test_soc_failure_threshold_defaults_to_three() -> None:
    assert Settings(ha_url="http://x", ha_token="t").soc_failure_threshold == 3


def test_soc_failure_threshold_is_configurable_across_surfaces() -> None:
    assert "soc_failure_threshold" in _OPTION_KEYS
    assert (
        Settings(
            ha_url="http://x", ha_token="t", soc_failure_threshold=5
        ).soc_failure_threshold
        == 5
    )


@pytest.mark.parametrize("bad", [0, -1])
def test_soc_failure_threshold_must_be_at_least_one(bad: int) -> None:
    with pytest.raises(ValidationError):
        Settings(ha_url="http://x", ha_token="t", soc_failure_threshold=bad)


def test_soc_recovery_minutes_defaults_to_ten_and_is_an_option() -> None:
    assert Settings(ha_url="http://x", ha_token="t").soc_recovery_minutes == 10
    assert "soc_recovery_minutes" in _OPTION_KEYS
    configured = Settings(ha_url="http://x", ha_token="t", soc_recovery_minutes=3)
    assert configured.soc_recovery_minutes == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_soc_recovery_minutes_must_be_at_least_one(bad: int) -> None:
    with pytest.raises(ValidationError):
        Settings(ha_url="http://x", ha_token="t", soc_recovery_minutes=bad)


def test_solis_fallback_current_is_optional_and_configured_across_surfaces() -> None:
    assert Settings(ha_url="http://x", ha_token="t").solis_fallback_current_a is None
    assert "solis_fallback_current_a" in _OPTION_KEYS
    assert (
        Settings(
            ha_url="http://x", ha_token="t", solis_fallback_current_a=45.0
        ).solis_fallback_current_a
        == 45.0
    )


@pytest.mark.parametrize("bad", [0.0, -1.0, 62.6])
def test_solis_fallback_current_must_fit_device_limits(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(ha_url="http://x", ha_token="t", solis_fallback_current_a=bad)


def test_inverter_clock_tolerance_stays_below_the_health_failure_threshold() -> None:
    with pytest.raises(ValidationError):
        Settings(inverter_clock_tolerance_minutes=31)


def test_addon_stop_timeout_lets_a_clean_shutdown_verify_its_safe_state() -> None:
    """#164: a clean shutdown makes two verified Solis writes (power switch, then
    Slot 1), each observed for up to the full read-back budget. The Supervisor
    kills the add-on after `timeout` (default 10 s), so it must cover both."""
    from ha_spark.devices.inverters import solis

    budget_s = solis._READ_BACK_ATTEMPTS * solis._READ_BACK_DELAY_SECONDS
    timeouts = [
        int(line.split(":", 1)[1])
        for line in ADDON_CONFIG.read_text(encoding="utf-8").splitlines()
        if line.startswith("timeout:")
    ]
    assert timeouts, "config.yaml sets no stop timeout; the Supervisor default is 10 s"
    assert timeouts[0] >= 2 * budget_s + 10


def test_export_event_provider_is_independent_of_tariff() -> None:
    from ha_spark.config import validate_axle_tariff

    assert Settings().export_event_provider == "none"
    validate_axle_tariff(Settings(axle_api_key="unused"))
    with pytest.raises(ConfigError, match="axle_api_key or axle_event_entity"):
        validate_axle_tariff(Settings(export_event_provider="axle"))
    validate_axle_tariff(
        Settings(tariff_provider="octopus_intelligent", export_event_provider="axle",
                 axle_event_entity="sensor.axle")
    )
    with pytest.raises(ValidationError):
        Settings(tariff_provider="axle")


def test_validate_options_document_names_field_without_its_value() -> None:
    """#412: a rejected options document names the failing field(s), never an
    input value (which can be a secret option or a nested devices entry)."""
    from ha_spark.config import validate_options_document

    with pytest.raises(ConfigError) as exc:
        validate_options_document(
            {"min_soc": "not-a-number", "octopus_api_key": "SECRET_OCTO"}
        )
    assert "min_soc" in str(exc.value)
    assert "not-a-number" not in str(exc.value)
    assert "SECRET_OCTO" not in str(exc.value)


def test_validate_options_document_honours_only_option_keys() -> None:
    """The document validator uses load_settings' overlay semantics: only
    ``_OPTION_KEYS`` are honoured and ``None`` values are dropped."""
    from ha_spark.config import validate_options_document

    settings = validate_options_document(
        {"min_soc": 30.0, "ha_token": "SNEAKY", "not_a_key": 1, "db_path": None}
    )
    assert settings.min_soc == 30.0
    assert settings.ha_token != "SNEAKY"
    assert settings.db_path == "data/ha_spark.db"


def test_validate_options_document_rejects_a_bad_tariff_naming_the_field() -> None:
    from ha_spark.config import validate_options_document

    with pytest.raises(ConfigError) as exc:
        validate_options_document({"charge_window_start": "25:99"})
    assert "window_start" in str(exc.value)
    assert "25:99" not in str(exc.value)
