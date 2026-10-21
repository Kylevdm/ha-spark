"""The settings service preserves fallback refusal, fencing and secret safety."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ha_spark.authority import Transition
from ha_spark.config import ConfigError
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


def test_save_fences_actuation_but_not_notification_changes(service):
    context = execution_context()
    generation = context.config_generation
    save(service, {"notify_service": "notify.phone"})
    assert context.config_generation == generation
    save(service, {"inverter_power_switch_entity": "select.new"})
    assert context.config_generation == generation + 1
    assert service.settings.inverter_power_switch_entity == "select.new"
