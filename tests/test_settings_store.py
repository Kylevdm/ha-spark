"""Owner-visible persistence, revisions and crash recovery of the settings pair."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark.config import ConfigError, Settings
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import SUPERVISOR_INFO_URL, SettingsService


@pytest.fixture(autouse=True)
def preserve_fence():
    context = execution_context()
    generation = context.config_generation
    try:
        yield
    finally:
        context.config_generation = generation


def store(tmp_path: Path) -> SettingsService:
    return SettingsService(
        tmp_path / "options.json",
        data_dir=tmp_path,
        settings=Settings(
            ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "ha_spark.db")
        ),
    )


def test_preview_writes_nothing_and_daemon_initialises_pair(tmp_path):
    service = store(tmp_path)
    service.load()
    assert list(tmp_path.iterdir()) == []
    service.load(writable=True)
    assert service.read_view()["revision"] == 1
    assert json.loads((tmp_path / "ha_spark_config.json").read_text()) == {
        "schema_version": 1,
        "revision": 1,
        "settings": {},
    }
    secrets = tmp_path / "ha_spark_secrets.json"
    assert json.loads(secrets.read_text()) == {"revision": 1, "secrets": {}}
    assert secrets.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "ha_spark_config.initialised").is_file()


@pytest.mark.parametrize("standalone", [False, True])
@pytest.mark.parametrize("source", ["env", "dotenv"])
def test_runtime_environment_cannot_override_seed_or_committed_pair(
    tmp_path, monkeypatch, standalone, source
):
    monkeypatch.chdir(tmp_path)
    for key in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN", "DB_PATH", "LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    values = {
        "MIN_SOC": "30",
        "OCTOPUS_API_KEY": "env-key",
        "TARIFF_PROVIDER": "invalid",
        "DEVICES": "not-json",
        "LOG_LEVEL": "DEBUG",
    }
    if standalone:
        values.update(
            HA_URL="http://ha.test", HA_TOKEN="test", DB_PATH=str(tmp_path / "ha_spark.db")
        )
    else:
        values["SUPERVISOR_TOKEN"] = "test"
    if source == "env":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    else:
        (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()))
        monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    options = tmp_path / "options.json"
    options.write_text('{"min_soc": 20, "tariff_provider": "fixed", "log_level": "WARNING"}')
    for writable in (True, False):
        service = SettingsService(options, data_dir=tmp_path)
        settings = service.load(writable=writable)
        assert settings.is_standalone is standalone
        assert settings.min_soc == 20
        assert settings.octopus_api_key == ""
        assert settings.tariff_provider == "fixed"
        assert settings.log_level == "WARNING"
        assert service.revision == 1
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["settings"] == {}
    assert json.loads((tmp_path / "ha_spark_secrets.json").read_text())["secrets"] == {}


def test_seed_drops_coerced_defaults(tmp_path):
    (tmp_path / "options.json").write_text('{"solar_percentile": "50"}')
    service = store(tmp_path)
    service.load(writable=True)
    assert json.loads((tmp_path / "ha_spark_config.json").read_text())["settings"] == {}


def test_seed_inputs_and_flat_remap(tmp_path):
    legacy = tmp_path / "options.json"
    legacy.write_text(
        json.dumps(
            {
                "min_soc": 20,
                "devices": [],
                "inverter_power_switch_entity": "select.old",
                "octopus_api_key": "CANARY-5f3a",
            }
        )
    )
    service = store(tmp_path)
    service.load(writable=True)
    before = legacy.read_bytes()
    result = service.save({"inverter_power_switch_entity": "select.new"}, 1, "seed")
    assert result == {"revision": 2, "changed": ["inverter_power_switch_entity"], "status": "live"}
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["settings"] == {"inverter_power_switch_entity": "select.new"}
    assert service.settings.devices[0].entities["power_switch"] == "select.new"
    assert legacy.read_bytes() == before
    assert "CANARY-5f3a" not in json.dumps(service.read_view())
    legacy.write_text('{"tariff_provider": "invalid"}')
    assert store(tmp_path).load().inverter_power_switch_entity == "select.new"


@pytest.mark.parametrize(
    "changes",
    [
        {"db_path": None},
        {"log_level": "DEBUG"},
        {"CANARY-5f3a": "x"},
        {"proactive_mode": "on"},
        {"tariff_provider": "CANARY-5f3a"},
        {"octopus_api_key": ""},
        {"octopus_api_key": {"secret": True, "set": True}},
    ],
)
def test_rejection_preserves_pair_snapshot_and_secrets(tmp_path, changes, caplog):
    service = store(tmp_path)
    service.load(writable=True)
    snapshot = service.settings
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ConfigError) as error:
        service.save(changes, 1, "seed")
    assert "CANARY-5f3a" not in str(error.value) + caplog.text
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert service.settings is snapshot


@pytest.mark.parametrize(
    "operation,step",
    [
        (operation, step)
        for operation, steps in (("replace", 2), ("fsync", 4), ("link", 2))
        for step in range(1, steps + 1)
    ],
)
def test_commit_fault_loads_one_complete_generation(tmp_path, monkeypatch, operation, step):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 21, "octopus_api_key": "old"}, 1, "seed")
    snapshot = service.settings
    original = getattr(os, operation)
    calls = 0

    def fault(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == step:
            raise OSError("CANARY-5f3a")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, operation, fault)
        with pytest.raises(ConfigError) as error:
            service.save({"min_soc": 22, "octopus_api_key": "new"}, 2, "seed")
        assert "CANARY-5f3a" not in str(error.value)
        assert service.settings is snapshot
        with pytest.raises(ConfigError, match="restart"):
            service.save({"min_soc": 23}, 2, "seed")
    loaded = store(tmp_path)
    settings = loaded.load()
    assert (settings.min_soc, settings.octopus_api_key) in {(21, "old"), (22, "new")}


def test_marked_missing_pair_never_reseeds(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    (tmp_path / "ha_spark_secrets.json").unlink()
    (tmp_path / "options.json").write_text('{"min_soc": 40}')
    with pytest.raises(ConfigError):
        store(tmp_path).load(writable=True)


def test_bad_seed_is_secret_safe_and_writes_nothing(tmp_path, caplog):
    path = tmp_path / "options.json"
    path.write_text('{"tariff_provider": "CANARY-5f3a"}')
    with pytest.raises(ConfigError, match="tariff_provider") as error:
        store(tmp_path).load(writable=True)
    assert "CANARY-5f3a" not in str(error.value) + caplog.text
    assert list(tmp_path.iterdir()) == [path]


# --- Supervisor seed fallback (#464) ------------------------------------------

CANARY = "CANARY-5f3a"


@pytest.fixture
def addon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap mode picks the seed sources; keep the environment out of it."""
    for var in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN", "LOG_LEVEL", "DB_PATH"):
        monkeypatch.delenv(var, raising=False)


def addon_store(tmp_path: Path, token: str = "sup-token") -> SettingsService:
    return SettingsService(
        tmp_path / "options.json",
        data_dir=tmp_path,
        settings=Settings(supervisor_token=token, ha_url="", ha_token=""),
    )


def supervisor_route(options: object) -> respx.Route:
    return respx.get(SUPERVISOR_INFO_URL).mock(
        return_value=httpx.Response(200, json={"result": "ok", "data": {"options": options}})
    )


@respx.mock
@pytest.mark.parametrize("options_file", [None, {}, {"log_level": "WARNING"}])
def test_supervisor_stored_options_seed_when_file_has_no_legacy_key(
    tmp_path, addon_env, options_file, caplog
):
    if options_file is not None:
        (tmp_path / "options.json").write_text(json.dumps(options_file))
    supervisor_route({
        "min_soc": 25,
        "octopus_api_key": CANARY,
        "proactive_mode": "on",
        f"{CANARY}-key": "value",
    })
    service = addon_store(tmp_path)
    settings = service.load(writable=True)
    assert settings.min_soc == 25
    assert settings.octopus_api_key == CANARY
    assert settings.log_level == ("WARNING" if options_file == {"log_level": "WARNING"} else "INFO")
    assert service.revision == 1
    assert (tmp_path / "ha_spark_config.initialised").is_file()
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["settings"] == {"min_soc": 25}
    assert json.loads((tmp_path / "ha_spark_secrets.json").read_text()) == {
        "revision": 1,
        "secrets": {"octopus_api_key": CANARY},
    }
    assert respx.calls[0].request.headers["Authorization"] == "Bearer sup-token"
    assert CANARY not in caplog.text


@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        "unauthorised",
        "server",
        "timeout",
        "connect",
        "invalid-json",
        "result-error",
        "data-not-dict",
        "options-list",
        "options-missing",
    ],
)
def test_supervisor_failure_falls_back_to_default_seed_without_echo(
    tmp_path, addon_env, failure, caplog
):
    route = respx.get(SUPERVISOR_INFO_URL)
    if failure == "unauthorised":
        route.mock(return_value=httpx.Response(401, json={"result": "error", "message": CANARY}))
    elif failure == "server":
        route.mock(return_value=httpx.Response(500, text=CANARY))
    elif failure == "timeout":
        route.mock(side_effect=httpx.ReadTimeout(CANARY))
    elif failure == "connect":
        route.mock(side_effect=httpx.ConnectError(CANARY))
    elif failure == "invalid-json":
        route.mock(return_value=httpx.Response(200, text='{"result": "ok", "data": ' + CANARY))
    elif failure == "result-error":
        route.mock(return_value=httpx.Response(200, json={"result": "error", "message": CANARY}))
    elif failure == "data-not-dict":
        route.mock(return_value=httpx.Response(200, json={"result": "ok", "data": CANARY}))
    elif failure == "options-list":
        route.mock(
            return_value=httpx.Response(200, json={"result": "ok", "data": {"options": [CANARY]}})
        )
    else:
        route.mock(return_value=httpx.Response(200, json={"result": "ok", "data": {}}))
    service = addon_store(tmp_path, token=f"token-{CANARY}")
    settings = service.load(writable=True)
    assert settings.min_soc == Settings().min_soc
    assert settings.octopus_api_key == ""
    assert service.revision == 1
    assert json.loads((tmp_path / "ha_spark_config.json").read_text())["settings"] == {}
    assert json.loads((tmp_path / "ha_spark_secrets.json").read_text())["secrets"] == {}
    assert "Supervisor options unavailable; seeding defaults" in caplog.text
    assert CANARY not in caplog.text


@respx.mock
@pytest.mark.parametrize(
    ("options_file", "expected_min_soc"),
    [({"min_soc": 25}, 25), ({"db_path": "placeholder"}, 20)],
)
def test_supervisor_not_consulted_when_options_file_holds_a_legacy_key(
    tmp_path, addon_env, options_file, expected_min_soc
):
    if options_file.get("db_path"):
        options_file["db_path"] = str(tmp_path / "ha_spark.db")
    (tmp_path / "options.json").write_text(json.dumps(options_file))
    route = supervisor_route({"min_soc": 30})
    service = addon_store(tmp_path)
    assert service.load(writable=True).min_soc == expected_min_soc
    assert route.call_count == 0


@respx.mock
def test_supervisor_not_consulted_after_initialisation_marker(tmp_path, addon_env):
    route = supervisor_route({"min_soc": 25})
    assert addon_store(tmp_path).load(writable=True).min_soc == 25
    assert route.call_count == 1
    (tmp_path / "options.json").write_text("{}")
    assert addon_store(tmp_path).load(writable=True).min_soc == 25
    assert route.call_count == 1


@respx.mock
def test_standalone_never_consults_the_supervisor(tmp_path, addon_env):
    route = respx.get(SUPERVISOR_INFO_URL)
    service = SettingsService(
        tmp_path / "options.json",
        data_dir=tmp_path,
        settings=Settings(
            ha_url="http://ha.test",
            ha_token="test",
            supervisor_token="sup-token",
            db_path=str(tmp_path / "ha_spark.db"),
        ),
    )
    service.load(writable=True)
    assert route.call_count == 0
    assert (tmp_path / "ha_spark_config.initialised").is_file()


@respx.mock
def test_supervisor_options_follow_the_fixed_db_path_rule(tmp_path, addon_env):
    supervisor_route({"min_soc": 25, "db_path": "custom/ha_spark.db"})
    with pytest.raises(ConfigError, match="move the database first"):
        addon_store(tmp_path).load(writable=True)
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_supervisor_status_text_never_reaches_the_log(tmp_path, addon_env, caplog):
    caplog.set_level("DEBUG")
    respx.get(SUPERVISOR_INFO_URL).mock(
        return_value=httpx.Response(500, extensions={"reason_phrase": CANARY.encode()})
    )
    addon_store(tmp_path).load(writable=True)
    assert CANARY not in caplog.text
