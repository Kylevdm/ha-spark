"""Config adapter boundaries reject untrusted payloads without echoing secrets."""
import asyncio
import io
import json
from pathlib import Path

import pytest

from ha_spark import cli
from ha_spark.config import Settings
from ha_spark.daemon_channel import MAX_CONFIG_BYTES, DaemonChannel, Reply
from ha_spark.devices.execution import execution_context
from tests.authority_helpers import seed_settings

CANARY = "CANARY-SECRET-123"


@pytest.fixture
def service(tmp_path):
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    result = seed_settings(tmp_path, settings=Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ha_spark.db"),
    ))
    try:
        yield result
    finally:
        context.authority, context.config_generation = authority, generation


@pytest.mark.parametrize("clear", [False, True])
def test_cli_secret_stdin_or_clear_never_echoed(service, monkeypatch, capsys, caplog, clear):
    calls = []

    async def submit(settings, op, **kwargs):
        calls.append((op, kwargs))
        return Reply(config={"revision": 7, "status": "live"})

    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(CANARY + "\n"))
    args = ["config", "set-secret", "octopus_api_key"] + (["--clear"] if clear else [])
    assert cli.main(args) == 0
    assert calls[-1] == ("config_set_secret", {"expected_revision": 7,
                         "field": "octopus_api_key", "value": {"clear": True} if clear else CANARY})
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err + caplog.text


def test_cli_secret_tty_uses_no_echo_prompt(service, monkeypatch, capsys):
    async def submit(settings, op, **kwargs):
        if op == "config_set_secret":
            assert kwargs["value"] == CANARY
        return Reply(config={"revision": 7})

    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "getpass", lambda prompt: CANARY)
    assert cli.main(["config", "set-secret", "axle_api_key"]) == 0
    assert CANARY not in capsys.readouterr().out


def test_cli_secret_argv_and_regular_set_refused_without_echo(monkeypatch, capsys):
    assert cli.main(["config", "set-secret", "axle_api_key", CANARY]) == 2
    assert cli.main(["config", "set", f"axle_api_key={CANARY}"]) == 2
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err


def test_cli_import_supervisor_yaml(service, monkeypatch, capsys):
    # The store can be isolated while the fixed bootstrap database is /data's default.
    service = seed_settings(service.data_dir, settings=Settings(
        ha_url="http://ha.test", ha_token="test", db_path="/data/ha_spark.db",
        solis_control_hub="solis_control",
    ))

    async def submit(settings, op, **kwargs):
        if op == "config_show":
            return Reply(config=service.read_view())
        assert op == "config_import"
        return Reply(config=service.import_document(
            kwargs["document"], kwargs["expected_revision"], "import",
        ))

    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "submit_socket", submit)
    fixture = Path(__file__).parent / "fixtures" / "supervisor-options-0.24.yaml"
    assert cli.main(["config", "import", str(fixture)]) == 0
    assert service.export()["charge_window_start"] == "23:30"
    assert "live" in capsys.readouterr().out


@pytest.mark.parametrize("content", [
    'octopus_api_key: ["CANARY-SECRET-123"',
    '{"octopus_api_key": "CANARY-SECRET-123"',
    "!!python/object/apply:os.system ['CANARY-SECRET-123']",
    "x" * (MAX_CONFIG_BYTES + 1),
])
def test_cli_invalid_import_sanitised(service, tmp_path, monkeypatch, capsys, content):
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    path = tmp_path / "options.yaml"
    path.write_text(content)
    assert cli.main(["config", "import", str(path)]) == 2
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err
    assert "REFUSED" in captured.out


@pytest.mark.parametrize("command", ["show", "export"])
def test_cli_read_without_daemon_is_non_writable(service, monkeypatch, capsys, command):
    async def submit(*args, **kwargs):
        return None

    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(cli, "submit_socket", submit)
    before = {p.name: p.read_bytes() for p in service.data_dir.iterdir()}
    assert cli.main(["config", command]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert before == {p.name: p.read_bytes() for p in service.data_dir.iterdir()}


@pytest.mark.parametrize("payload", [
    {"op": "config_save", "expected_revision": 1, "changes": {}, "ingress_user": CANARY},
    {"op": "config_save", "changes": {}},
    {"op": "config_set_secret", "expected_revision": 1, "field": CANARY, "value": CANARY},
    {"op": "config_save", "expected_revision": True, "changes": {}},
    {"op": "config_import", "expected_revision": 1, "document": {"x": "x" * MAX_CONFIG_BYTES}},
])
async def test_socket_rejects_envelopes_and_size_cap(service, payload):
    async with DaemonChannel(service.settings) as channel:
        channel.settings_service = service
        reader, writer = await asyncio.open_unix_connection(str(channel.path))
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), 2)
        assert b"invalid daemon request" in reply
        assert CANARY.encode() not in reply
        assert channel.queue.empty()
        writer.close()
        await writer.wait_closed()
