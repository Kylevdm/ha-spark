from pathlib import Path

import pytest

from ha_spark.agent.auth import resolve_token, verify
from ha_spark.config import Settings


def _settings(**kw: object) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="x", **kw)  # type: ignore[call-arg]


def test_configured_token_wins(tmp_path: Path) -> None:
    tok = resolve_token(_settings(agent_api_token="abc"), tmp_path / "agent_token")
    assert tok == "abc"


def test_generates_and_persists(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "agent_token"
    with caplog.at_level("DEBUG", logger="ha_spark.agent.auth"):
        first = resolve_token(_settings(), path)
    assert first and path.read_text().strip() == first
    assert first not in caplog.text
    assert str(path) in caplog.text
    assert resolve_token(_settings(), path) == first  # stable across calls


def test_verify() -> None:
    assert verify("Bearer abc", "abc") is True
    assert verify("Bearer wrong", "abc") is False
    assert verify(None, "abc") is False
    assert verify("abc", "abc") is False  # missing scheme


@pytest.mark.parametrize(("header", "token"), [("Bearer sékret", "sekret"), ("Bearer x", "sékret")])
def test_verify_refuses_non_ascii_without_raising(header: str, token: str) -> None:
    assert verify(header, token) is False
