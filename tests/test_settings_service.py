"""The settings service preserves fallback refusal, fencing and secret safety."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ha_spark.authority import Transition
from ha_spark.config import ConfigError, Settings, field_metadata
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import SettingsService
from tests.authority_helpers import seed_authority, seed_settings


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsService]:
    monkeypatch.chdir(tmp_path)
    for name in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test")
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    result = seed_settings(
        tmp_path,
        {
            "solis_fallback_current_a": 20,
            "inverter_power_switch_entity": "select.old",
            "octopus_api_key": "secret-canary",
        },
    )
    try:
        yield result
    finally:
        context.authority = authority
        context.config_generation = generation


def pair(service):
    return {
        name: (service.data_dir / name).read_bytes()
        for name in ("ha_spark_config.json", "ha_spark_secrets.json")
    }


def save(service, changes):
    return service.save(changes, service.revision, "seed")


def test_load_overlays_options_and_can_skip_validation(service, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    fresh = SettingsService(service.options_path, data_dir=service.data_dir)
    with pytest.raises(ConfigError, match="No Home Assistant credentials"):
        fresh.load()
    settings = fresh.load(validate=False)
    assert settings.inverter_power_switch_entity == "select.old"
    assert fresh.settings is settings


def test_read_view_masks_only_set_secrets(service):
    view = service.read_view()["settings"]
    assert view["octopus_api_key"] == {"secret": True, "set": True}
    assert view["axle_api_key"] == {"secret": True, "set": False}
    assert "secret-canary" not in json.dumps(view)
    assert isinstance(view["devices"][0], dict)


def test_save_merges_and_preserves_masked_secret(service):
    save(service, {"notify_service": "notify.phone", "octopus_api_key": "***"})
    document = json.loads((service.data_dir / "ha_spark_config.json").read_text())
    secrets = json.loads((service.data_dir / "ha_spark_secrets.json").read_text())
    assert secrets["secrets"]["octopus_api_key"] == "secret-canary"
    assert document["settings"]["inverter_power_switch_entity"] == "select.old"
    assert service.settings.notify_service == "phone"


@pytest.mark.parametrize("authority", ["optimise", "transition", "unknown", "failed"])
@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"solis_fallback_current_a": None}, "solis_fallback_current_a"),
        ({"max_charge_current_a": 10}, "max_charge_current_a"),
        ({"inverter": "alphaess"}, "inverter configuration"),
    ],
)
def test_fallback_refusal_preserves_file_and_snapshot(service, authority, changes, reason):
    store = seed_authority(service.settings, "optimise" if authority == "optimise" else "observe")
    if authority == "transition":
        store.save(
            store.state.model_copy(
                update={
                    "transition": Transition(
                        target="optimise",
                        started_at=datetime.now(UTC),
                    )
                }
            )
        )
    elif authority == "unknown":
        store.store_path.unlink()
    elif authority == "failed":
        store.save_failed = True
    before = pair(service)
    settings = service.settings
    authority_bytes = store.store_path.read_bytes() if store.store_path.exists() else None
    with pytest.raises(ConfigError, match=reason):
        save(service, changes)
    assert pair(service) == before
    assert service.settings is settings
    assert (store.store_path.read_bytes() if store.store_path.exists() else None) == authority_bytes


@pytest.mark.parametrize("mode", ["observe", "protect"])
def test_fallback_edit_is_allowed_without_optimise_authority(service, mode):
    seed_authority(service.settings, mode)
    save(service, {"solis_fallback_current_a": None})
    assert service.settings.solis_fallback_current_a is None


def test_unknown_authority_allows_install_without_valid_fallback(service):
    seed_authority(service.settings, "observe")
    save(service, {"solis_fallback_current_a": None})
    store = seed_authority(service.settings, "observe")
    store.store_path.unlink()
    save(service, {"notify_service": "notify.phone"})
    assert service.settings.notify_service == "phone"


@pytest.mark.parametrize("value", [None, "secret-canary"])
def test_db_path_refused_before_validation_without_echo(service, value):
    before = pair(service)
    settings = service.settings
    with pytest.raises(ConfigError, match="db_path cannot be changed") as error:
        save(service, {"db_path": value, "tariff_provider": "secret-canary"})
    assert "secret-canary" not in str(error.value)
    assert pair(service) == before
    assert service.settings is settings


def test_invalid_candidate_is_secret_safe_and_changes_nothing(service):
    before = pair(service)
    settings = service.settings
    with pytest.raises(ConfigError, match="tariff_provider") as error:
        save(service, {"tariff_provider": "secret-canary"})
    assert "secret-canary" not in str(error.value)
    assert pair(service) == before
    assert service.settings is settings


def test_restart_save_is_all_or_nothing_and_applies_after_restart(service):
    snapshot = service.settings
    generation = execution_context().config_generation
    result = save(service, {
        "agent_api_token": "replacement-secret",
        "solis_modbus_slave": 2,
        "inverter_power_switch_entity": "select.new",
    })
    assert result["status"] == "pending_restart"
    assert service.settings is snapshot
    assert execution_context().config_generation == generation
    view = service.read_view()
    assert view["status"] == "pending_restart"
    assert view["pending_restart_fields"] == ["agent_api_token", "solis_modbus_slave"]
    assert view["settings"]["agent_api_token"] == {"secret": True, "set": False}
    assert "replacement-secret" not in json.dumps(view)

    assert save(service, {"min_soc": 25})["status"] == "pending_restart"
    assert service.settings is snapshot
    assert execution_context().config_generation == generation
    fresh = SettingsService(service.options_path, data_dir=service.data_dir)
    fresh.load(writable=True)
    assert fresh.settings.agent_api_token == "replacement-secret"
    assert fresh.settings.solis_modbus_slave == 2
    assert fresh.settings.inverter_power_switch_entity == "select.new"
    assert fresh.settings.min_soc == 25
    assert fresh.read_view()["status"] == "live"
    assert fresh.read_view()["pending_restart_fields"] == []


def test_live_save_requests_replan_but_pending_save_does_not(service):
    snapshots = []
    service.request_replan = lambda: snapshots.append(service.settings)
    result = save(service, {"inverter_power_switch_entity": "select.new"})
    assert result["status"] == "live"
    assert snapshots == [service.settings]
    save(service, {"solis_modbus_slave": 2})
    save(service, {"min_soc": 25})
    assert len(snapshots) == 1


@pytest.mark.parametrize("transition", [False, True])
def test_startup_rechecks_fallback_against_current_authority(service, transition):
    service = seed_settings(service.data_dir, settings=Settings(
        ha_url="http://ha.test", ha_token="test",
        db_path=str(service.data_dir / "ha_spark.db"),
    ))
    seed_authority(service.settings, "observe")
    save(service, {"solis_modbus_slave": 2, "solis_fallback_current_a": None})
    store = seed_authority(service.settings, "observe" if transition else "optimise")
    if transition:
        store.save(store.state.model_copy(update={"transition": Transition(
            target="optimise", started_at=datetime.now(UTC),
        )}))
    before = {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}
    execution_context().authority = None
    fresh = SettingsService(
        service.options_path, settings=service.settings, data_dir=service.data_dir,
    )
    with pytest.raises(ConfigError, match="solis_fallback_current_a"):
        fresh.load(writable=True)
    assert {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()} == before
    assert store.store_path.read_bytes() == before[store.store_path.name]


@pytest.mark.parametrize("writable", [False, True])
def test_startup_without_pending_marker_does_not_recheck_fallback(service, writable):
    service = seed_settings(service.data_dir, settings=Settings(
        ha_url="http://ha.test", ha_token="test",
        db_path=str(service.data_dir / "ha_spark.db"),
    ))
    seed_authority(service.settings, "observe")
    save(service, {"solis_fallback_current_a": None})
    seed_authority(service.settings, "optimise")
    execution_context().authority = None
    fresh = SettingsService(
        service.options_path, settings=service.settings, data_dir=service.data_dir,
    )
    assert fresh.load(writable=writable).solis_fallback_current_a is None


def test_read_only_load_leaves_pending_marker_and_skips_guard(service):
    seed_authority(service.settings, "observe")
    save(service, {"solis_modbus_slave": 2, "solis_fallback_current_a": None})
    seed_authority(service.settings, "optimise")
    marker = service.data_dir / "ha_spark_config.pending"
    assert marker.exists()
    before = {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()}
    fresh = SettingsService(service.options_path, data_dir=service.data_dir)
    assert fresh.load().solis_modbus_slave == 2
    assert {p.name: p.read_bytes() for p in service.data_dir.iterdir() if p.is_file()} == before


def test_unknown_startup_authority_accepts_pending_and_removes_marker(service):
    seed_authority(service.settings, "observe")
    save(service, {"solis_modbus_slave": 2, "solis_fallback_current_a": None})
    execution_context().authority = None
    fresh = SettingsService(service.options_path, data_dir=service.data_dir)
    assert fresh.load(writable=True).solis_modbus_slave == 2
    assert not (service.data_dir / "ha_spark_config.pending").exists()


def test_pending_marker_write_failure_refuses_save(service, monkeypatch):
    import os

    original = os.replace

    def fail_marker(source, destination):
        if Path(destination).name == "ha_spark_config.pending":
            raise OSError("secret-canary")
        original(source, destination)

    before = pair(service)
    snapshot = service.settings
    monkeypatch.setattr(os, "replace", fail_marker)
    with pytest.raises(ConfigError, match="Configuration save failed; restart required") as error:
        save(service, {"solis_modbus_slave": 2})
    assert "secret-canary" not in str(error.value)
    assert service.save_failed
    assert pair(service) == before
    assert service.settings is snapshot
    assert not (service.data_dir / "ha_spark_config.pending").exists()


@pytest.mark.parametrize("field,value", [
    ("agent_api_token", "replacement-secret"),
    ("agent_expose_port", True),
    ("agent_surface", "on"),
    ("alphaess_serial", "replacement-serial"),
    ("devices", [{"id": "new", "driver": "solis"}]),
    ("inverter", "alphaess"),
    ("solis_control_hub", "replacement-hub"),
    ("solis_modbus_slave", 2),
])
def test_each_restart_field_defers_complete_snapshot(service, field, value):
    assert field_metadata()[field]["reload"] == "restart"
    seed_authority(service.settings, "observe")
    snapshot = service.settings
    generation = execution_context().config_generation
    result = save(service, {field: value, "min_soc": 25})
    assert result["status"] == "pending_restart"
    assert service.settings is snapshot
    assert execution_context().config_generation == generation
    assert field in service.read_view()["pending_restart_fields"]
    assert (service.data_dir / "ha_spark_config.pending").exists()
    fresh = SettingsService(service.options_path, data_dir=service.data_dir)
    fresh.load(writable=True)
    assert getattr(fresh.settings, field) != getattr(snapshot, field)
    assert fresh.settings.min_soc == 25
    assert fresh.read_view()["status"] == "live"
    assert not (service.data_dir / "ha_spark_config.pending").exists()


def test_save_fences_actuation_but_not_notification_changes(service):
    context = execution_context()
    generation = context.config_generation
    save(service, {"notify_service": "notify.phone"})
    assert context.config_generation == generation
    save(service, {"inverter_power_switch_entity": "select.new"})
    assert context.config_generation == generation + 1
    assert service.settings.inverter_power_switch_entity == "select.new"


def test_reverting_the_pending_restart_change_goes_live_again(service):
    running = service.settings.solis_modbus_slave
    assert save(service, {"solis_modbus_slave": running + 1})["status"] == "pending_restart"
    result = save(service, {"solis_modbus_slave": running, "min_soc": 25})
    assert result["status"] == "live"
    assert service.settings.min_soc == 25
    assert service.read_view()["pending_restart_fields"] == []


def test_removing_a_running_restart_input_stays_pending(service):
    assert save(service, {"solis_modbus_slave": 2})["status"] == "pending_restart"
    restarted = SettingsService(service.options_path, data_dir=service.data_dir)
    restarted.load(writable=True)
    snapshot = restarted.settings
    # Dropping the input reverts to the default: a restart change even though
    # neither the old nor the new committed inputs hold the key afterwards.
    assert save(restarted, {"solis_modbus_slave": None})["status"] == "pending_restart"
    assert save(restarted, {"min_soc": 25})["status"] == "pending_restart"
    assert restarted.settings is snapshot


def test_read_view_carries_field_metadata(service):
    metadata = service.read_view()["metadata"]
    assert metadata["solis_modbus_slave"] == {"owner": "integration", "secret": False,
                                              "reload": "restart"}
    assert metadata["octopus_api_key"]["secret"] is True
