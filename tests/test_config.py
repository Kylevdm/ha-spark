from ha_agent.config import Settings


def test_websocket_url_derivation_http() -> None:
    s = Settings(ha_url="http://homeassistant.local:8123/")
    assert s.ha_websocket_url == "ws://homeassistant.local:8123/api/websocket"
    assert s.ha_rest_url == "http://homeassistant.local:8123/api"


def test_websocket_url_derivation_https() -> None:
    s = Settings(ha_url="https://ha.example.com")
    assert s.ha_websocket_url == "wss://ha.example.com/api/websocket"


def test_defaults_present() -> None:
    s = Settings()
    assert s.ollama_num_ctx == 32768
    assert s.db_path.endswith(".db")
