"""The 0.31 empty hub default: pre-0.31 legacy Solis stores and exports keep `solis_control`."""

import json
import logging
from pathlib import Path

import pytest

from ha_spark.authority import HoldDevice
from ha_spark.devices.execution import execution_context, held_identity_remapped
from ha_spark.settings_service import ConfigurationRepair
from tests.test_settings_store import store


def old_store(tmp_path: Path, inputs: dict) -> None:
    """A store committed by 0.25-0.30: its marker is an empty document."""
    (tmp_path / "ha_spark_config.initialised").write_text("{}")
    (tmp_path / "ha_spark_config.json").write_text(json.dumps({
        "schema_version": 1, "revision": 7, "settings": inputs,
    }))
    (tmp_path / "ha_spark_secrets.json").write_text(json.dumps({
        "revision": 7, "secrets": {},
    }))


def files(tmp_path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.fixture(autouse=True)
def _keep_generation():
    generation = execution_context().config_generation
    yield
    execution_context().config_generation = generation


def test_fresh_install_hub_defaults_empty(tmp_path):
    service = store(tmp_path)
    assert service.load(writable=True).solis_control_hub == ""
    assert service.revision == 1
    restarted = store(tmp_path)
    assert restarted.load(writable=True).solis_control_hub == ""
    assert restarted.revision == 1


@pytest.mark.parametrize("hub", ["", "custom", "solis_control"])
def test_explicit_hub_is_kept_and_not_migrated(tmp_path, hub):
    old_store(tmp_path, {"solis_control_hub": hub})
    service = store(tmp_path)
    assert service.load(writable=True).solis_control_hub == hub
    assert service.revision == 7
    assert not (tmp_path / "ha_spark_config.json.prev").exists()


def test_pre_031_store_without_hub_migrates_once(tmp_path, caplog):
    old_store(tmp_path, {"min_soc": 22})
    before = (tmp_path / "ha_spark_config.json").read_bytes()
    generation = execution_context().config_generation
    service = store(tmp_path)
    with caplog.at_level(logging.INFO):
        assert service.load(writable=True).solis_control_hub == "solis_control"
    assert "fields=solis_control_hub actor=migration" in caplog.text
    assert service.revision == 8
    assert not service.pending_restart
    assert execution_context().config_generation == generation
    assert (tmp_path / "ha_spark_config.json.prev").read_bytes() == before
    document = json.loads((tmp_path / "ha_spark_config.json").read_text())
    assert document["settings"] == {"min_soc": 22, "solis_control_hub": "solis_control"}
    after = files(tmp_path)
    restarted = store(tmp_path)
    assert restarted.load(writable=True).solis_control_hub == "solis_control"
    assert restarted.revision == 8
    assert files(tmp_path) == after


def test_cleared_hub_after_migration_stays_empty(tmp_path):
    old_store(tmp_path, {})
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"solis_control_hub": None}, service.revision, "cli")
    restarted = store(tmp_path)
    assert restarted.load(writable=True).solis_control_hub == ""
    assert restarted.revision == 9


def test_read_only_load_previews_migration_without_writing(tmp_path):
    old_store(tmp_path, {})
    before = files(tmp_path)
    assert store(tmp_path).load().solis_control_hub == "solis_control"
    assert files(tmp_path) == before


def test_alphaess_store_not_migrated(tmp_path):
    old_store(tmp_path, {"inverter": "alphaess"})
    service = store(tmp_path)
    assert service.load(writable=True).solis_control_hub == ""
    assert service.revision == 7


def test_migration_keeps_held_identity(tmp_path):
    old_store(tmp_path, {"inverter_power_switch_entity": "select.owner"})
    settings = store(tmp_path).load(writable=True)
    held = HoldDevice(driver="solis", power_switch="select.owner", hub="solis_control", slave=1)
    assert not held_identity_remapped(held, settings)


def test_0_24_seed_keeps_explicit_hub(tmp_path):
    (tmp_path / "options.json").write_text('{"solis_control_hub": "solis_control"}')
    service = store(tmp_path)
    assert service.load(writable=True).solis_control_hub == "solis_control"
    assert service.export()["solis_control_hub"] == "solis_control"


@pytest.mark.parametrize("document, expected", [({}, "solis_control"),
                                               ({"inverter": "alphaess"}, ""),
                                               ({"solis_control_hub": ""}, ""),
                                               ({"solis_control_hub": "custom"}, "custom")])
def test_pre_031_import_migrates_missing_hub(tmp_path, document, expected):
    service = store(tmp_path)
    service.load(writable=True)
    service.import_document(document, service.revision, "import")
    assert store(tmp_path).load(writable=True).solis_control_hub == expected


def test_pre_031_repair_import_migrates_missing_hub(tmp_path):
    old_store(tmp_path, {})
    (tmp_path / "ha_spark_secrets.json").write_text("not json")
    service = store(tmp_path)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    service.import_document({"min_soc": 22}, service.repair_token(), "import")
    assert service.settings.solis_control_hub == "solis_control"
    restarted = store(tmp_path)
    assert restarted.load(writable=True).solis_control_hub == "solis_control"
    assert restarted.revision == service.revision


def test_new_export_round_trip_keeps_empty_default(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    document = service.export()
    assert document == {"solis_control_hub": ""}
    service.import_document(document, service.revision, "import")
    assert service.settings.solis_control_hub == ""
    assert service.export() == document

