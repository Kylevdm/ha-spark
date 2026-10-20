"""Read-only mode readiness, driven through HA responses and CLI output."""
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_spark import cli
from ha_spark.config import DeviceConfig, Settings
from ha_spark.energy.soc_integrity import check_soc
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.mode_preconditions import check_preconditions
from tests.authority_helpers import seed_authority

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def settings(tmp_path, **kwargs):
    return Settings(ha_url="http://ha.test", ha_token="secret", db_path=str(tmp_path / "db"),
                    devices=[DeviceConfig(id="solis", type="inverter", driver="solis",
                                          entities={"power_switch": "select.power"})],
                    ev_status_entity="sensor.ev", battery_power_entity="sensor.battery",
                    solis_fallback_current_a=10, **kwargs)


def ha_response(request):
    entity = request.url.path.rsplit("/", 1)[-1]
    value = "on" if entity == "select.power" else (
        "38" if entity.endswith("model_word") else
        "8241" if entity.endswith("inverter_type_definition") else "0")
    return httpx.Response(200, json={"entity_id": entity, "state": value,
                                    "attributes": {}, "last_reported": NOW.isoformat()})


def measurement():
    return check_soc(EntityState(entity_id="sensor.soc", state="50", attributes={},
                                 last_reported=NOW), observed_at=NOW, max_age=timedelta(minutes=10))


@respx.mock
async def test_preconditions_pass_using_model_probe_and_checked_soc(tmp_path):
    route = respx.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=ha_response)
    async with HomeAssistantRest("http://ha.test/api", "secret") as rest:
        report = await check_preconditions(settings(tmp_path), rest, measurement())
    assert report.protect == () and report.optimise == ()
    assert route.called
    assert all(call.request.method == "GET" for call in respx.calls)


@pytest.mark.parametrize("case,reason", [
    ("alphaess", "no validated hold surface (#196)"),
    ("multiple", "exactly one inverter"),
    ("model", "differs from reference"),
    ("switch", "power_switch"),
    ("age", "ev_status_max_age_seconds"),
    ("fallback", "solis_fallback_current_a"),
    ("soc", "SoC"),
])
@respx.mock
async def test_preconditions_name_each_failure(tmp_path, case, reason):
    config = settings(tmp_path)
    updates = {}
    if case == "alphaess":
        updates["devices"] = [config.devices[0].model_copy(update={"driver": "alphaess"})]
    elif case == "multiple":
        updates["devices"] = config.devices * 2
    elif case == "age":
        updates["ev_status_max_age_seconds"] = None
    elif case == "fallback":
        updates["solis_fallback_current_a"] = None
    config = config.model_copy(update=updates)
    if case == "model":
        respx.get(url__regex=r".*model_word$").respond(200, json={
            "entity_id": "sensor.model_word", "state": "1", "attributes": {}})
    if case == "switch":
        respx.get("http://ha.test/api/states/select.power").respond(200, json={
            "entity_id": "select.power", "state": "unavailable", "attributes": {}})
    # respx takes the first matching route: the specific overrides go first.
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=ha_response)
    async with HomeAssistantRest("http://ha.test/api", "secret") as rest:
        report = await check_preconditions(config, rest, None if case == "soc" else measurement())
    assert any(reason in failure for failure in report.optimise)
    assert all(call.request.method == "GET" for call in respx.calls)


@respx.mock
async def test_preconditions_report_all_missing_evidence_and_fallback(tmp_path):
    config = settings(tmp_path).model_copy(update={
        "ev_status_entity": "", "battery_power_entity": "",
        "dispatch_entity": "sensor.dispatch", "live_dispatch_max_age_seconds": None,
        "solis_fallback_current_a": None,
    })
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=ha_response)
    async with HomeAssistantRest("http://ha.test/api", "secret") as rest:
        report = await check_preconditions(config, rest, None)
    for reason in ("ev_status_entity", "battery_power_entity", "live_dispatch_max_age_seconds",
                   "solis_fallback_current_a", "SoC"):
        assert any(reason in failure for failure in report.optimise)
    assert len(report.protect) == 3


@pytest.mark.parametrize("known", [False, True])
@respx.mock
def test_mode_show_reports_targets_without_writes(tmp_path, monkeypatch, capsys, known):
    config = settings(tmp_path)
    if known:
        seed_authority(config, "protect")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: config)
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=ha_response)
    assert cli.main(["mode", "show"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["preconditions"]["protect"]["passed"] is True
    assert output["preconditions"]["optimise"]["passed"] is False
    assert all(call.request.method == "GET" for call in respx.calls)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert output["mode"] == ("protect" if known else "authority_unknown")
