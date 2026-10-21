"""The settings service preserves the legacy options store and safety gates."""
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
from tests.authority_helpers import seed_authority


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsService]:
    monkeypatch.chdir(tmp_path)
    for name in ("HA_URL", "HA_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test")
    path = tmp_path / "options.json"
    path.write_text(json.dumps({
        "solis_fallback_current_a": 20,
        "inverter_power_switch_entity": "select.old",
        "octopus_api_key": "secret-canary",
    }))
    result = SettingsService(path)
    result.load()
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    try:
        yield result
    finally:
        context.authority = authority
        context.config_generation = generation


def test_load_overlays_options_and_can_skip_validation(service, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    with pytest.raises(ConfigError, match="No Home Assistant credentials"):
        service.load()
    settings = service.load(validate=False)
    assert settings.inverter_power_switch_entity == "select.old"
    assert service.settings is settings


def test_read_view_masks_only_set_secrets(service):
    view = service.read_view()
    assert view["octopus_api_key"] == "***"
    assert view["axle_api_key"] == ""
    assert "secret-canary" not in json.dumps(view)
    assert isinstance(view["devices"][0], dict)


def test_save_merges_ignores_unknown_keys_and_preserves_masked_secret(service):
    settings = service.save({
        "notify_service": "notify.phone", "octopus_api_key": "***", "unknown": "ignored",
    })
    document = json.loads(service.options_path.read_text())
    assert document["octopus_api_key"] == "secret-canary"
    assert document["inverter_power_switch_entity"] == "select.old"
    assert "unknown" not in document
    assert settings.notify_service == "phone"
    assert service.settings is settings


@pytest.mark.parametrize("authority", ["optimise", "transition", "unknown", "failed"])
@pytest.mark.parametrize("changes,reason", [
    ({"solis_fallback_current_a": None}, "solis_fallback_current_a"),
    ({"max_charge_current_a": 10}, "max_charge_current_a"),
    ({"inverter": "alphaess"}, "inverter configuration"),
])
def test_fallback_refusal_preserves_file_and_snapshot(service, authority, changes, reason):
    store = seed_authority(service.settings, "optimise" if authority == "optimise" else "observe")
    if authority == "transition":
        store.save(store.state.model_copy(update={"transition": Transition(
            target="optimise", started_at=datetime.now(UTC),
        )}))
    elif authority == "unknown":
        store.store_path.unlink()
    elif authority == "failed":
        store.save_failed = True
    before = service.options_path.read_bytes()
    settings = service.settings
    authority_bytes = store.store_path.read_bytes() if store.store_path.exists() else None
    with pytest.raises(ConfigError, match=reason):
        service.save(changes)
    assert service.options_path.read_bytes() == before
    assert service.settings is settings
    assert (store.store_path.read_bytes() if store.store_path.exists() else None) == authority_bytes


@pytest.mark.parametrize("mode", ["observe", "protect"])
def test_fallback_edit_is_allowed_without_optimise_authority(service, mode):
    seed_authority(service.settings, mode)
    assert service.save({"solis_fallback_current_a": None}).solis_fallback_current_a is None


def test_unknown_authority_allows_install_without_valid_fallback(service):
    service.options_path.write_text("{}")
    service.load()
    store = seed_authority(service.settings, "observe")
    store.store_path.unlink()
    assert service.save({"notify_service": "notify.phone"}).notify_service == "phone"


@pytest.mark.parametrize("value", [None, "secret-canary"])
def test_db_path_refused_before_validation_without_echo(service, value):
    before = service.options_path.read_bytes()
    settings = service.settings
    with pytest.raises(ConfigError, match="db_path cannot be changed") as error:
        service.save({"db_path": value, "tariff_provider": "secret-canary"})
    assert "secret-canary" not in str(error.value)
    assert service.options_path.read_bytes() == before
    assert service.settings is settings


def test_invalid_candidate_is_secret_safe_and_changes_nothing(service):
    before = service.options_path.read_bytes()
    settings = service.settings
    with pytest.raises(ConfigError, match="tariff_provider") as error:
        service.save({"tariff_provider": "secret-canary"})
    assert "secret-canary" not in str(error.value)
    assert service.options_path.read_bytes() == before
    assert service.settings is settings


def test_save_fences_actuation_but_not_notification_changes(service):
    context = execution_context()
    generation = context.config_generation
    service.save({"notify_service": "notify.phone"})
    assert context.config_generation == generation
    service.save({"inverter_power_switch_entity": "select.new"})
    assert context.config_generation == generation + 1
    assert service.settings.inverter_power_switch_entity == "select.new"
