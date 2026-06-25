import json
from pathlib import Path

import pytest

from ha_spark.config import (
    _OPTION_KEYS,
    SUPERVISOR_REST_URL,
    SUPERVISOR_WS_URL,
    ConfigError,
    DeviceConfig,
    Settings,
    _read_options_overlay,
    load_settings,
)
from ha_spark.devices.base import ControlAuthority

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
    monkeypatch.setattr("ha_spark.config._read_options_overlay", lambda: {})
    with pytest.raises(ConfigError):
        load_settings()


def test_load_settings_ok_in_addon_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    monkeypatch.setattr("ha_spark.config._read_options_overlay", lambda: {})
    settings = load_settings()
    assert settings.is_standalone is False
    assert settings.auth_token == "sup-token"


def test_load_settings_validate_false_skips_credential_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Diagnostic paths (`ha-spark health`) must build settings without creds
    # while still honouring the options overlay.
    monkeypatch.setattr(
        "ha_spark.config._read_options_overlay",
        lambda: {"ollama_url": "http://100.1.2.3:11434"},
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


def test_addon_schema_covers_all_option_keys() -> None:
    """Every honoured option key appears in the add-on schema, and vice versa.

    Only top-level (exactly 2-space-indented) schema keys count; nested keys
    under ``devices:`` (deeper indentation) are skipped.
    """
    in_schema = False
    schema_keys: set[str] = set()
    for line in ADDON_CONFIG.read_text(encoding="utf-8").splitlines():
        if line == "schema:":
            in_schema = True
            continue
        if in_schema:
            if not line.startswith("  "):
                break
            if line.startswith("   "):  # 3+ spaces => nested; skip
                continue
            schema_keys.add(line.split(":", 1)[0].strip())
    assert schema_keys == set(_OPTION_KEYS)


def test_agent_defaults() -> None:
    s = Settings(ha_url="http://ha.test", ha_token="x")  # type: ignore[call-arg]
    assert s.agent_surface == "off"
    assert s.agent_exposure == "read_act"
    assert s.agent_expose_port is False
    assert s.agent_api_token == ""


def test_agent_options_in_whitelist() -> None:
    for key in ("agent_surface", "agent_exposure", "agent_api_token", "agent_expose_port"):
        assert key in _OPTION_KEYS


def test_devices_synthesized_from_flat_keys_when_absent() -> None:
    s = Settings(
        supervisor_token="sup",
        inverter="solis",
        charge_current_entity="number.cc",
        charge_window_start_entity="time.ws",
        charge_window_end_entity="time.we",
        inverter_power_switch_entity="select.pw",
    )  # type: ignore[call-arg]
    assert len(s.devices) == 1
    d = s.devices[0]
    assert isinstance(d, DeviceConfig)
    assert d.id == "main_inverter"
    assert d.type == "inverter"
    assert d.driver == "solis"
    assert d.control == ControlAuthority.HA_SPARK
    assert d.entities["charge_current"] == "number.cc"
    assert d.entities["window_start"] == "time.ws"


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
    assert s.devices[0].control == ControlAuthority.OBSERVE


def test_devices_in_option_keys() -> None:
    assert "devices" in _OPTION_KEYS
