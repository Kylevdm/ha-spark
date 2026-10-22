"""Retired configuration cannot supply site authority (#417)."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ha_spark import config, settings_service
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore
from ha_spark.devices.execution import ExecutionContext, Ownership
from tests.api_helpers import owner_client


@pytest.mark.parametrize("keys", ["proactive_mode", "control", "both"])
async def test_retired_options_load_once_without_grant(tmp_path, monkeypatch, caplog, keys):
    options = {}
    if keys in ("proactive_mode", "both"):
        options["proactive_mode"] = "on"
    if keys in ("control", "both"):
        options["devices"] = [
            {"id": "main", "type": "inverter", "driver": "solis", "control": "ha_spark"},
            {"id": "other", "type": "inverter", "driver": "solis", "control": "secret-value"},
        ]
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options))
    monkeypatch.setenv("HA_URL", "http://ha.test")
    monkeypatch.setenv("HA_TOKEN", "test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "events.db"))
    read_overlay = settings_service._read_options_overlay
    monkeypatch.setattr(settings_service, "_read_options_overlay", lambda _: read_overlay(path))
    settings = config.load_settings()
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "deprecated" in warnings[0].lower()
    assert "secret-value" not in warnings[0] and "ha_spark" not in warnings[0]
    assert ("proactive_mode" in warnings[0]) == (keys != "control")
    assert ("control" in warnings[0]) == (keys != "proactive_mode")
    assert "proactive_mode" not in config.Settings.model_fields
    assert all("control" not in d.model_dump() for d in settings.devices)
    store = AuthorityStore(settings)
    store.initialise()
    assert store.state.committed_mode == "observe"
    context = ExecutionContext()
    context.authority = store
    context.planned_epoch.set(store.state.epoch)
    context.planned_generation.set(context.config_generation)
    assert context.gate.mode_for(Ownership.CHARGING, settings.devices[0], settings) == "simulate"
    assert store.state.committed_mode == "observe"

    from ha_spark.devices.inverters.solis import SolisDevice
    from tests.test_solis_power_switch import FakeRest, _intent

    rest = FakeRest()
    await SolisDevice(settings.devices[0], settings, rest, execution=context).apply(_intent())
    assert rest.calls == []
    assert store.state.committed_mode == "observe"


def test_retired_fields_not_exposed():
    assert "proactive_mode" not in config._OPTION_KEYS
    assert "proactive_mode" not in config.Settings.model_fields
    assert "control" not in config.DeviceConfig.model_fields


@pytest.mark.parametrize("surface", ["/api/config", "/agent/config", "mcp"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
async def test_config_post_drops_retired_key_and_sidecar(tmp_path: Path, surface, existing, mode):
    path = tmp_path / "options.json"
    settings = config.Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "events.db"),
        agent_surface="on", agent_exposure="read_write",
    )
    store = AuthorityStore(settings)
    store.initialise()
    store.save(store.state.model_copy(update={"committed_mode": mode}))
    before = store.store_path.read_bytes()
    legacy = {
        "proactive_mode": "simulate",
        "devices": [{"id": "main", "type": "inverter", "driver": "solis", "control": "ha_spark"}],
    }
    if existing:
        path.write_text(json.dumps(legacy))
    from tests.authority_helpers import seed_settings

    service = seed_settings(tmp_path, settings=settings)
    state = AppState(service.settings, path, settings_service=service)
    if surface == "mcp":
        from mcp.server.fastmcp.exceptions import ToolError

        from ha_spark.agent.mcp_server import build_mcp

        # Agents can't change configuration at all (#457).
        with pytest.raises(ToolError):
            await build_mcp(state).call_tool("set_config", {"updates": {"proactive_mode": "on"}})
    elif surface == "/agent/config":
        with TestClient(build_app(state)) as client:
            response = client.post(surface, json={"proactive_mode": "on"})
        assert response.status_code == 409
    else:
        with owner_client(state) as client:
            response = client.post(surface, json={"expected_revision": service.revision,
                                                  "changes": {"proactive_mode": "on"}})
        assert response.status_code == 400
        assert "retired: use the operating mode" in response.json()["error"]
    assert store.store_path.read_bytes() == before
    if existing:
        assert json.loads(path.read_text()) == legacy
    else:
        assert not path.exists()
    assert not (tmp_path / "proactive_mode.txt").exists()


@pytest.mark.parametrize("mode", ["observe", "protect", "optimise"])
async def test_published_payloads_report_committed_mode(tmp_path, monkeypatch, mode):
    from ha_spark.energy import publish
    from ha_spark.energy.soc_monitor import SocMonitor
    from tests.authority_helpers import seed_authority
    from tests.test_publish import _plan, _soc

    settings = config.Settings(
        ha_url="http://ha.test", ha_token="test", db_path=str(tmp_path / "events.db")
    )
    seed_authority(settings, mode)
    entities = publish.plan_to_payload(_plan(), settings)
    captured = []

    async def capture(rest, payload):
        captured.extend(payload)

    monkeypatch.setattr(publish, "_push", capture)
    snapshot = SocMonitor().record(_soc(40), failure_threshold=3)
    await publish.publish_soc_integrity(None, snapshot, settings)
    await publish.publish_predictions(None, [], settings)
    by_id = {eid: attrs for eid, _, attrs in entities + captured}
    for eid in (
        "sensor.ha_spark_plan_status",
        "sensor.ha_spark_soc_integrity",
        "sensor.ha_spark_predictions",
    ):
        assert by_id[eid]["operating_mode"] == mode
        assert "proactive_mode" not in by_id[eid]
