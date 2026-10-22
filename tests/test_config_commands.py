"""Owner config imports and CLI commands use the single daemon writer."""
import json

import pytest

from ha_spark import cli
from ha_spark.config import ConfigError, Settings
from ha_spark.daemon_channel import DaemonChannel, Reply, submit_socket
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import RevisionConflict
from tests.authority_helpers import seed_authority, seed_settings


@pytest.fixture
def service(tmp_path):
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    result = seed_settings(
        tmp_path, {"octopus_api_key": "CANARY-SECRET-123"},
        settings=Settings(ha_url="http://ha.test", ha_token="test",
                          db_path=str(tmp_path / "ha_spark.db")),
    )
    try:
        yield result
    finally:
        context.authority, context.config_generation = authority, generation


def test_import_replaces_inputs_preserves_secrets_and_roundtrips(service):
    service.save({"notify_service": "phone"}, service.revision, "cli")
    service.import_document({"log_level": "info", "db_path": service.settings.db_path,
                             "target_soc_cap": 85}, service.revision, "import")
    assert service.settings.notify_service == ""
    assert service.settings.octopus_api_key == "CANARY-SECRET-123"
    exported = service.export()
    assert exported == {"target_soc_cap": 85}
    service.import_document(exported, service.revision, "import")
    assert service.export() == exported


def test_import_refusals_leave_files_unchanged(service):
    before = {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}
    for document, reason in [({"db_path": "/elsewhere/db"}, "move the database"),
                             ({"CANARY-SECRET-123": 1}, "Unknown"),
                             ({"tariff_provider": "CANARY-SECRET-123"}, "tariff_provider")]:
        with pytest.raises(ConfigError, match=reason):
            service.import_document(document, service.revision, "import")
    with pytest.raises(RevisionConflict):
        service.import_document({}, service.revision - 1, "import")
    assert before == {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}


async def test_config_socket_operations(service, caplog):
    async with DaemonChannel(service.settings) as channel:
        channel.settings_service = service
        shown = await submit_socket(service.settings, "config_show")
        assert shown.config["revision"] == service.revision
        reply = await submit_socket(service.settings, "config_set_secret",
                                    expected_revision=service.revision, field="axle_api_key",
                                    value="CANARY-SECRET-123")
        assert reply.error is None
        exported = await submit_socket(service.settings, "config_export")
        assert "CANARY-SECRET-123" not in json.dumps(exported.model_dump())
        assert "CANARY-SECRET-123" not in json.dumps(reply.model_dump())
        saved = await submit_socket(service.settings, "config_save",
                                    expected_revision=service.revision,
                                    changes={"notify_service": "x" * 8192})
        assert saved.error is None
        assert len(service.settings.notify_service) == 8192
        imported = await submit_socket(service.settings, "config_import",
                                       expected_revision=service.revision, document={})
        assert imported.error is None
        assert service.export() == {}
        assert service.settings.axle_api_key == "CANARY-SECRET-123"
        conflict = await submit_socket(service.settings, "config_save",
                                       expected_revision=service.revision - 1, changes={})
        assert conflict.error == "conflict"
        assert conflict.config == {"revision": service.revision}
        assert channel.queue.empty()
    assert "CANARY-SECRET-123" not in caplog.text


def test_import_uses_fallback_guard_and_leaves_authority_unchanged(service):
    service.save({"solis_fallback_current_a": 20}, service.revision, "cli")
    store = seed_authority(service.settings, "optimise")
    execution_context().authority = store
    before = {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}
    snapshot = service.settings
    with pytest.raises(ConfigError, match="Invalid fallback charging schedule"):
        service.import_document({}, service.revision, "import")
    assert service.settings is snapshot
    assert before == {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}


def test_import_secret_clear_is_explicit_and_mask_means_unchanged(service):
    for document in ({}, {"octopus_api_key": ""}, {"octopus_api_key": "***"}):
        service.import_document(document, service.revision, "import")
        assert service.read_view()["settings"]["octopus_api_key"]["set"] is True
    service.import_document({"octopus_api_key": {"clear": True}}, service.revision, "import")
    assert service.read_view()["settings"]["octopus_api_key"]["set"] is False


def test_cli_set_reads_revision_and_reports_conflict(service, monkeypatch, capsys):
    calls = []

    async def submit(settings, op, **kwargs):
        calls.append((op, kwargs))
        if op == "config_show":
            return Reply(config={"revision": 4})
        return Reply(error="conflict", config={"revision": 5})

    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    assert cli.main(["config", "set", "target_soc_cap=85"]) == 1
    assert calls[1] == ("config_save", {"expected_revision": 4,
                                         "changes": {"target_soc_cap": 85}})
    assert "conflict" in capsys.readouterr().out


async def test_cli_import_nondefault_db_path_refused_without_writes(
    service, tmp_path, monkeypatch, capsys,
):
    import asyncio

    document = tmp_path / "legacy.json"
    document.write_text(json.dumps({"db_path": str(tmp_path / "elsewhere" / "ha_spark.db")}))
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    snapshot = service.settings
    async with DaemonChannel(service.settings) as channel:
        channel.settings_service = service
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
        assert await asyncio.to_thread(cli.main, ["config", "import", str(document)]) != 0
        captured = capsys.readouterr()
        assert "move the database" in captured.out + captured.err
        assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
        assert service.settings is snapshot


@pytest.mark.parametrize("command", ["import", "set-secret"])
def test_cli_import_and_secret_refuse_without_daemon_before_reading_stdin(
    service, tmp_path, monkeypatch, capsys, command,
):
    import sys

    class UnreadableStdin:
        def isatty(self):
            raise AssertionError("secret input must not be inspected without a daemon")

        def readline(self, *args):
            raise AssertionError("secret input must not be read without a daemon")

        def read(self, *args):
            raise AssertionError("secret input must not be read without a daemon")

    async def submit(*args, **kwargs):
        return None

    document = tmp_path / "import.json"
    document.write_text("{}")
    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    monkeypatch.setattr(sys, "stdin", UnreadableStdin())
    argument = str(document) if command == "import" else "octopus_api_key"
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert cli.main(["config", command, argument]) != 0
    assert cli.REFUSED in capsys.readouterr().out
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_cli_no_daemon_writes_refused(service, monkeypatch, capsys):
    async def submit(*args, **kwargs):
        return None

    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli, "Settings", lambda: service.settings)
    assert cli.main(["config", "set", "target_soc_cap=85"]) == 1
    assert "REFUSED" in capsys.readouterr().out
