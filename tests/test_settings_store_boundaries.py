"""Seed compatibility, restart recovery and sanitised service/API boundaries."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest
import respx
import yaml
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.api.server import AppState, build_app
from ha_spark.config import _SECRET_OPTION_KEYS, ConfigError, Settings
from ha_spark.energy import scheduler
from ha_spark.settings_service import RevisionConflict
from tests.api_helpers import OWNER_CONFIG_HEADERS
from tests.test_settings_store import preserve_fence as preserve_fence
from tests.test_settings_store import store

CANARY = "CANARY-5f3a"
FIXTURE = Path(__file__).parent / "fixtures" / "addon-config-0.24.yaml"


@pytest.mark.parametrize("edited", [False, True])
def test_legacy_fixture_seeds_equivalent_effective_settings(tmp_path, edited):
    options = yaml.load(FIXTURE.read_text(), Loader=yaml.BaseLoader)["options"]
    options["db_path"] = str(tmp_path / "ha_spark.db")
    if edited:
        options.update(
            {
                "min_soc": 25,
                "inverter_power_switch_entity": "select.owner",
                "octopus_api_key": CANARY,
                "axle_api_key": CANARY,
                "agent_api_token": CANARY,
                "proactive_mode": "on",
            }
        )
    (tmp_path / "options.json").write_text(json.dumps(options))
    service = store(tmp_path)
    actual = service.load(writable=True)
    expected = Settings(**(options | {"ha_url": "http://ha.test", "ha_token": "test"}))
    assert actual.model_dump() == expected.model_dump()
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["settings"] == (
        {"min_soc": 25, "inverter_power_switch_entity": "select.owner"} if edited else {}
    )
    assert CANARY not in json.dumps(document)


def test_tariff_refusal_carries_no_input_in_its_exception_chain(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    with pytest.raises(ConfigError) as excinfo:
        service.save({"charge_window_start": CANARY}, 1, "seed")
    chain = "".join(traceback.format_exception(excinfo.value))
    assert "charge_window_start" in chain
    assert CANARY not in chain


@pytest.mark.parametrize("field", sorted(_SECRET_OPTION_KEYS))
def test_secret_wire_forms_and_clear_survive_restart(tmp_path, field, caplog):
    service = store(tmp_path)
    service.load(writable=True)
    result = service.save({field: CANARY}, 1, "seed")
    assert result["revision"] == 2
    service.save({field: "***", "min_soc": 22}, 2, "seed")
    service.save({"min_soc": None}, 3, "seed")
    assert getattr(store(tmp_path).load(), field) == CANARY
    assert service.read_view()["settings"][field] == {"secret": True, "set": True}
    service.save({field: {"clear": True}}, 4, "seed")
    fresh = store(tmp_path)
    assert getattr(fresh.load(), field) == ""
    assert fresh.read_view()["settings"][field] == {"secret": True, "set": False}
    assert CANARY not in json.dumps(result) + json.dumps(fresh.read_view()) + caplog.text


def test_stale_revision_conflict_changes_nothing(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    snapshot = service.settings
    with pytest.raises(RevisionConflict) as error:
        service.save({"min_soc": 22}, 0, "seed")
    assert error.value.revision == 1
    assert service.settings is snapshot
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize(
    "payload",
    [
        '{"expected_revision": 1, "changes": {"' + CANARY + '": "x"}}',
        '{"expected_revision": true, "changes": {}}',
        '{"expected_revision": "1", "changes": {}}',
        '{"expected_revision": 1, "changes": {}, "' + CANARY + '": 1}',
        '{"expected_revision": 1, "changes": {"tariff_provider": "' + CANARY + '"}}',
        '{"' + CANARY + '": invalid JSON}',
    ],
)
def test_ingress_bad_bodies_never_echo_input_or_change_pair(tmp_path, payload, caplog):
    service = store(tmp_path)
    service.load(writable=True)
    state = AppState(service.settings, service.options_path, settings_service=service)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with TestClient(build_app(state, ingress_peer="testclient")) as client:
        response = client.post(
            "/api/config",
            content=payload,
            headers=OWNER_CONFIG_HEADERS | {"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert CANARY not in response.text + caplog.text
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_ingress_stale_revision_returns_current_revision(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    state = AppState(service.settings, service.options_path, settings_service=service)
    with TestClient(
        build_app(state, ingress_peer="testclient"), headers=OWNER_CONFIG_HEADERS
    ) as client:
        body = {"expected_revision": 1, "changes": {"min_soc": 22}}
        assert client.post("/api/config", json=body).json()["status"] == "live"
        response = client.post("/api/config", json=body)
    assert response.status_code == 409
    assert response.json() == {"error": "conflict", "revision": 2}


@pytest.mark.parametrize("operation,steps", [("replace", 3), ("fsync", 5)])
def test_seed_crash_before_or_after_marker_restarts_safely(tmp_path, monkeypatch, operation, steps):
    for step in range(1, steps + 1):
        directory = tmp_path / str(step)
        directory.mkdir()
        legacy = directory / "options.json"
        legacy.write_text(json.dumps({"min_soc": 23, "octopus_api_key": CANARY}))
        service = store(directory)
        original = getattr(os, operation)
        calls = 0

        def fault(*args, _step=step, _original=original, **kwargs):
            nonlocal calls
            calls += 1
            if calls == _step:
                raise OSError(CANARY)
            return _original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, operation, fault)
            with pytest.raises(ConfigError) as error:
                service.load(writable=True)
            assert CANARY not in str(error.value)
        restarted = store(directory)
        settings = restarted.load(writable=True)
        assert (settings.min_soc, settings.octopus_api_key) == (23, CANARY)
        assert restarted.revision == 1
        assert (directory / "ha_spark_config.initialised").is_file()


def test_read_only_mismatch_recovery_does_not_write(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 22, "octopus_api_key": "old"}, 1, "seed")
    path = tmp_path / "ha_spark_secrets.json"
    document = json.loads(path.read_text())
    document["revision"] = 3
    document["secrets"]["octopus_api_key"] = "new"
    path.write_text(json.dumps(document))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    # The previous pair is revision 1 (defaults), never the mixed current pair.
    fresh = store(tmp_path)
    assert fresh.load().min_soc == 20
    assert fresh.settings.octopus_api_key == ""
    assert fresh.revision == 1
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    fresh.load(writable=True)
    assert json.loads(path.read_text())["revision"] == 1


@pytest.mark.parametrize(
    "content,reason",
    [
        ('{"' + CANARY + '": invalid}', "JSON"),
        (json.dumps({"schema_version": 2, "revision": 1, "settings": {}}), "schema_version"),
        (
            json.dumps(
                {"schema_version": 1, "revision": 1, "settings": {"tariff_provider": CANARY}}
            ),
            "tariff_provider",
        ),
    ],
)
def test_invalid_marked_document_fails_cleanly(tmp_path, content, reason, caplog):
    service = store(tmp_path)
    service.load(writable=True)
    (tmp_path / "ha_spark_config.json").write_text(content)
    with pytest.raises(ConfigError, match=reason) as error:
        store(tmp_path).load()
    assert CANARY not in str(error.value) + caplog.text


def test_unreadable_store_fails_cleanly_without_reseed(tmp_path, monkeypatch, caplog):
    store(tmp_path).load(writable=True)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path.name == "ha_spark_secrets.json":
            raise OSError(CANARY)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ConfigError, match="unreadable") as error:
        store(tmp_path).load(writable=True)
    assert CANARY not in str(error.value) + caplog.text
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_secrets_temporary_is_private_before_first_write(tmp_path, monkeypatch):
    original = os.fdopen
    checked = []

    def check(fd, *args, **kwargs):
        assert os.fstat(fd).st_mode & 0o777 == 0o600
        assert os.fstat(fd).st_size == 0
        checked.append(fd)
        return original(fd, *args, **kwargs)

    (tmp_path / "options.json").write_text(json.dumps({"octopus_api_key": CANARY}))
    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", check)
        store(tmp_path).load(writable=True)
    assert len(checked) == 3  # document, secrets and marker; all private from creation


def test_read_only_save_refused_without_creating_files(tmp_path):
    service = store(tmp_path)
    service.load()
    with pytest.raises(ConfigError, match="running daemon"):
        service.save({"min_soc": 22}, 0, "seed")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("db_path", [None, CANARY])
def test_nondefault_legacy_db_path_refused_before_any_write(tmp_path, db_path):
    (tmp_path / "options.json").write_text(json.dumps({"db_path": db_path, "min_soc": 23}))
    with pytest.raises(ConfigError, match="move the database first") as error:
        store(tmp_path).load(writable=True)
    assert CANARY not in str(error.value)
    assert [p.name for p in tmp_path.iterdir()] == ["options.json"]


@pytest.mark.parametrize(
    "changes",
    [
        {"devices": ""},
        {"devices": False},
        {"octopus_api_key": False},
        {"axle_api_key": []},
    ],
)
def test_invalid_falsy_legacy_values_are_not_silently_dropped(tmp_path, changes):
    (tmp_path / "options.json").write_text(json.dumps(changes))
    with pytest.raises(ConfigError):
        store(tmp_path).load(writable=True)
    assert [p.name for p in tmp_path.iterdir()] == ["options.json"]


def test_writable_load_never_skips_whole_candidate_validation(tmp_path):
    (tmp_path / "options.json").write_text('{"charge_window_start": "invalid"}')
    with pytest.raises(ConfigError, match="window_start"):
        store(tmp_path).load(validate=False, writable=True)
    assert [p.name for p in tmp_path.iterdir()] == ["options.json"]


def test_unmarked_pair_and_temporary_files_are_not_trusted(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 40}, 1, "seed")
    (tmp_path / "ha_spark_config.initialised").unlink()
    (tmp_path / ".ha_spark_secrets.json.crashed").write_text(CANARY)
    (tmp_path / "options.json").write_text('{"min_soc": 23}')
    fresh = store(tmp_path)
    assert fresh.load().min_soc == 23
    assert (tmp_path / ".ha_spark_secrets.json.crashed").exists()
    assert fresh.load(writable=True).min_soc == 23
    assert fresh.revision == 1
    assert not (tmp_path / ".ha_spark_secrets.json.crashed").exists()
    assert not (tmp_path / "ha_spark_config.json.prev").exists()


def test_mismatched_pair_without_previous_fails_cleanly(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    (tmp_path / "ha_spark_secrets.json").write_text('{"revision": 2, "secrets": {}}')
    with pytest.raises(ConfigError, match="revision mismatch"):
        store(tmp_path).load(writable=True)


def test_invalid_previous_pair_is_not_restored_before_validation(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 22}, 1, "seed")
    (tmp_path / "ha_spark_secrets.json").write_text('{"revision": 3, "secrets": {}}')
    previous = tmp_path / "ha_spark_config.json.prev"
    document = json.loads(previous.read_text())
    document["settings"]["tariff_provider"] = CANARY
    previous.write_text(json.dumps(document))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError, match="tariff_provider"):
        store(tmp_path).load(writable=True)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("missing_secret", [False, True])
def test_newer_schema_is_never_overwritten_from_previous_pair(tmp_path, missing_secret):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 22}, 1, "seed")
    path = tmp_path / "ha_spark_config.json"
    document = json.loads(path.read_text())
    document["schema_version"] = 2
    path.write_text(json.dumps(document))
    if missing_secret:
        (tmp_path / "ha_spark_secrets.json").unlink()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError, match="schema_version"):
        store(tmp_path).load(writable=True)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_seed_unknown_keys_are_counted_without_echo(tmp_path, caplog):
    (tmp_path / "options.json").write_text(json.dumps({CANARY: "value", "min_soc": 23}))
    assert store(tmp_path).load(writable=True).min_soc == 23
    assert "Ignoring 1 unknown legacy options" in caplog.text
    assert CANARY not in caplog.text


@pytest.mark.parametrize("initialised", [False, True])
def test_invalid_bootstrap_log_level_is_secret_safe(tmp_path, initialised):
    if initialised:
        store(tmp_path).load(writable=True)
    (tmp_path / "options.json").write_text(json.dumps({"log_level": CANARY}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError, match="log_level") as error:
        store(tmp_path).load(writable=True)
    assert CANARY not in str(error.value)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@respx.mock
@pytest.mark.parametrize("commit_failure", [False, True])
def test_run_config_failure_exits_two_without_echo_or_hardware(
    tmp_path,
    monkeypatch,
    capsys,
    commit_failure,
):
    service = store(tmp_path)
    if commit_failure:

        def fail(*args, **kwargs):
            raise OSError(CANARY)

        monkeypatch.setattr(os, "replace", fail)
    else:
        (tmp_path / "options.json").write_text(json.dumps({"tariff_provider": CANARY}))
    monkeypatch.setattr(cli, "load_settings", lambda: service.load())
    monkeypatch.setattr(cli, "setup_logging", lambda level: None)
    monkeypatch.setattr(scheduler, "OPTIONS_PATH", tmp_path / "options.json")
    assert cli.main(["run"]) == 2
    output = capsys.readouterr()
    assert "Configuration error:" in output.err
    assert CANARY not in output.err + output.out
    assert not (tmp_path / "ha_spark_config.initialised").exists()


def test_only_log_level_remains_a_supervisor_bootstrap_option(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    (tmp_path / "options.json").write_text('{"log_level": "DEBUG", "min_soc": 40}')
    settings = store(tmp_path).load()
    assert settings.min_soc == 20
    assert settings.log_level == "DEBUG"
