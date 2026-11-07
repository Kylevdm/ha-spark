"""Stored writer metadata is tolerated, validated and exposed only in repair reasons."""

import json

import pytest

from ha_spark import settings_service
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import ConfigurationRepair
from tests.test_settings_store import store

MALFORMED = [
    "v0.32.0", "0.32.0\n", "0.32", "0.32.0-CANARY", 32, None,
    ["0.32.0"], "1" * 40, "１.2.3",
]


@pytest.fixture(autouse=True)
def preserve_generation(monkeypatch):
    context = execution_context()
    monkeypatch.setattr(context, "config_generation", context.config_generation)


@pytest.fixture
def seeded(tmp_path):
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 22}, 1, "seed")
    return service


@pytest.mark.parametrize("writer", ["9.9.9", "0.32.0", *MALFORMED, "absent"])
def test_written_by_unknown_fields_reason(tmp_path, seeded, monkeypatch, writer):
    monkeypatch.setattr(settings_service, "__version__", "0.32.0")
    path = tmp_path / "ha_spark_config.json"
    document = json.loads(path.read_text())
    document["settings"]["future_setting"] = True
    if writer != "absent":
        document["written_by"] = writer
    path.write_text(json.dumps(document))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    expected = "Unknown configuration fields: 1"
    if writer == "9.9.9":
        expected += " (written by ha-spark 9.9.9; this is 0.32.0)"
    service = store(tmp_path)
    with pytest.raises(ConfigurationRepair) as caught:
        service.load(writable=True)
    assert str(caught.value) == expected
    assert service.repair == [expected]
    assert service.read_view()["reasons"] == [expected]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("writer", ["9.9.9", *MALFORMED])
def test_written_by_without_unknown_fields_loads_cleanly(tmp_path, seeded, writer):
    path = tmp_path / "ha_spark_config.json"
    document = json.loads(path.read_text())
    document["written_by"] = writer
    path.write_text(json.dumps(document))
    service = store(tmp_path)
    assert service.load(writable=True) == seeded.settings
    assert service.revision == seeded.revision
    assert service.repair is None


@pytest.mark.parametrize("writer", [False, True])
@pytest.mark.parametrize("filename", ["ha_spark_config.json", "ha_spark_secrets.json"])
def test_other_envelope_keys_still_refused(tmp_path, seeded, writer, filename):
    path = tmp_path / filename
    document = json.loads(path.read_text())
    document["unexpected"] = True
    if writer:
        document["written_by"] = "9.9.9"
    path.write_text(json.dumps(document))
    service = store(tmp_path)
    with pytest.raises(ConfigurationRepair) as caught:
        service.load(writable=True)
    assert str(caught.value) == "Invalid configuration envelope"


def test_written_by_not_allowed_in_secrets(tmp_path, seeded):
    path = tmp_path / "ha_spark_secrets.json"
    document = json.loads(path.read_text())
    document["written_by"] = "9.9.9"
    path.write_text(json.dumps(document))
    with pytest.raises(ConfigurationRepair) as caught:
        store(tmp_path).load(writable=True)
    assert str(caught.value) == "Invalid configuration envelope"


def test_commit_does_not_write_written_by(tmp_path, seeded):
    path = tmp_path / "ha_spark_config.json"
    document = json.loads(path.read_text())
    document["written_by"] = "9.9.9"
    path.write_text(json.dumps(document))
    service = store(tmp_path)
    service.load(writable=True)
    service.save({"min_soc": 23}, service.revision, "cli")
    assert json.loads(path.read_text()) == {
        "schema_version": 1, "revision": 3, "settings": {"min_soc": 23},
    }
    assert json.loads((tmp_path / "ha_spark_secrets.json").read_text()) == {
        "revision": 3, "secrets": {},
    }


@pytest.mark.parametrize("writer", ["9.9.9", "0.32.0-CANARY"])
def test_previous_pair_with_written_by_still_recovers(tmp_path, seeded, writer):
    # The previous document is a committed backup: it accepts the same envelope.
    (tmp_path / "ha_spark_secrets.json").write_text('{"revision": 3, "secrets": {}}')
    previous = tmp_path / "ha_spark_config.json.prev"
    document = json.loads(previous.read_text())
    document["written_by"] = writer
    previous.write_text(json.dumps(document))
    service = store(tmp_path)
    service.load(writable=True)
    assert service.repair is None
    assert service.settings.min_soc != 22
    assert "written_by" not in json.loads((tmp_path / "ha_spark_config.json").read_text())


@pytest.mark.parametrize("writer", [False, True])
def test_previous_pair_with_other_envelope_key_is_not_recovered(tmp_path, seeded, writer):
    (tmp_path / "ha_spark_secrets.json").write_text('{"revision": 3, "secrets": {}}')
    previous = tmp_path / "ha_spark_config.json.prev"
    document = json.loads(previous.read_text())
    document["unexpected"] = True
    if writer:
        document["written_by"] = "9.9.9"
    previous.write_text(json.dumps(document))
    with pytest.raises(ConfigurationRepair) as caught:
        store(tmp_path).load(writable=True)
    assert str(caught.value) == "Configuration revision mismatch"
