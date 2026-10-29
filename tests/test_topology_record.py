"""Durable native acceptance is shared by daemon and read-only callers."""

import json
from pathlib import Path

import pytest

from ha_spark.config import Settings
from ha_spark.energy.topology import Topology, validate_topology
from ha_spark.energy.topology_record import load_topology_record, save_topology_record

FIXTURES = Path(__file__).parent / "fixtures" / "contract"


def topology():
    raw = json.loads((FIXTURES.parent / "limits" / "maximum_topology.json").read_text())
    return raw.get("topology", raw)


def test_restart_reads_accepted_record(tmp_path):
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "db"))
    doc = validate_topology(topology())
    assert isinstance(doc, Topology)
    assert load_topology_record(settings).kind == "legacy"
    save_topology_record(settings, 1, doc)
    loaded = load_topology_record(settings)
    assert loaded.kind == "native" and loaded.revision == 1 and loaded.topology == doc


@pytest.mark.parametrize("body", ["not json", '{"schema_version":2}', "x" * 32769])
@pytest.mark.parametrize("marker", [False, True])
def test_corrupt_record_never_loses_native_acceptance(tmp_path, body, marker):
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "db"))
    (tmp_path / "ha_spark_topology.json").write_text(body)
    if marker:
        (tmp_path / "ha_spark_topology.initialised").write_text("corrupt marker still counts")
    loaded = load_topology_record(settings)
    assert loaded.kind == ("unavailable" if marker else "legacy")


def test_marker_without_record_fails_closed(tmp_path):
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "db"))
    (tmp_path / "ha_spark_topology.initialised").touch()
    loaded = load_topology_record(settings)
    assert loaded.kind == "unavailable"
    assert loaded.reason == "topology_record_unavailable"


@pytest.fixture
def only_topology_check(monkeypatch):
    """Stub every other health check so run_health runs offline, unchanged otherwise."""
    from ha_spark import health

    async def quiet(settings, *args):
        return health.CheckResult("Stub", health.Status.OK, "")

    for name in dir(health):
        if name.startswith("check_") and name != "check_topology_record":
            if name in ("check_hold_release", "check_entity_config"):
                stub = (lambda s: []) if name == "check_hold_release" else (
                    lambda s: health.CheckResult("Stub", health.Status.OK, ""))
                monkeypatch.setattr(health, name, stub)
            elif name == "check_evidence_freshness":
                async def none(settings):
                    return []
                monkeypatch.setattr(health, name, none)
            else:
                monkeypatch.setattr(health, name, quiet)


@pytest.mark.parametrize("marker_only", [False, True])
async def test_cli_and_agent_read_the_daemon_record(
    tmp_path, capsys, only_topology_check, marker_only,
):
    from ha_spark import cli, health
    from ha_spark.agent import tools
    from ha_spark.api import server

    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "db"))
    if marker_only:
        (tmp_path / "ha_spark_topology.initialised").touch()
        expected = "unavailable; topology_record_unavailable"
    else:
        doc = validate_topology(topology())
        assert isinstance(doc, Topology)
        save_topology_record(settings, 3, doc)  # the daemon's intake writer
        expected = "native; revision 3"
    # One loader for every reader: daemon seed, health (CLI) and agent tool.
    assert server.load_topology_record is load_topology_record
    assert health.load_topology_record is load_topology_record
    daemon = server.AppState(settings, tmp_path / "options.json").integration_link.topology
    assert daemon == load_topology_record(settings)

    await cli._cmd_health(settings)
    printed = [line for line in capsys.readouterr().out.splitlines() if "Topology" in line]
    agent = [c for c in (await tools.get_health(settings)).checks if c["name"] == "Topology"]
    assert len(printed) == 1 and printed[0].endswith(expected)
    assert len(agent) == 1 and agent[0]["detail"] == expected
