import pytest

from ha_spark.config import (
    SUPERVISOR_REST_URL,
    SUPERVISOR_WS_URL,
    ConfigError,
    Settings,
    load_settings,
)


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
