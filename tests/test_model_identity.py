"""Read-only model qualification and resident schedule diagnostics (#418)."""
import json

import httpx
import pytest
import respx
from pydantic import ValidationError

from ha_spark.config import Settings
from ha_spark.devices import inverter_device
from ha_spark.ha.rest import HomeAssistantRest


async def inspect(values, *, confirmed=False, driver="solis"):
    settings = Settings(
        ha_url="http://ha.test", ha_token="fixture", inverter=driver,
        solis_model_confirmed=confirmed, inverter_power_switch_entity="select.power",
    )
    with respx.mock(assert_all_called=False) as mock:
        def state(request):
            entity = request.url.path.rsplit("/", 1)[-1]
            value = values.get(entity, None)
            if value is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"entity_id": entity, "state": str(value)})
        mock.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=state)
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            device = inverter_device(settings, rest)
            validation = await device.hold_validation()
            resident = await device.resident_diagnostic()
        assert all(call.request.method == "GET" for call in mock.calls)
    return validation, resident


def identity(model=38, definition=8241, tou=0):
    return {f"sensor.solis_control_{key}": value for key, value in {
        "model_word": model, "inverter_type_definition": definition, "tou_version": tou,
    }.items()}


@pytest.mark.parametrize("model,definition,tou,valid", [
    (38, 8241, 0, True), (39, 8241, 0, False), (38, 8240, 0, False),
    (38, 8241, 43605, False), (38, 8241, None, True),
])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_identity_both_words_and_v2_override_confirmation(
    model, definition, tou, valid, confirmed,
):
    result, _ = await inspect(identity(model, definition, tou), confirmed=confirmed)
    assert result.validated is valid
    if valid:
        assert result.contract is not None
        assert all((result.contract.hold, result.contract.release,
                    result.contract.read_back, result.contract.evidence_freshness))
    else:
        assert result.contract is None
        assert "0x" in result.reason
        assert "not validated" in result.reason


@pytest.mark.parametrize("bad", [None, "unavailable", "unknown", "garbage", "NaN", 38.5, -1, 65536])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_unreadable_requires_owner_confirmation(bad, confirmed):
    result, _ = await inspect(identity(model=bad), confirmed=confirmed)
    assert result.validated is confirmed
    assert result.identity_source == ("owner confirmation" if confirmed else "unreadable")


async def test_partial_mismatch_is_not_overridden():
    result, _ = await inspect(identity(model=None, definition=8240), confirmed=True)
    assert not result.validated
    assert "35000=0x2030" in result.reason


async def test_alphaess_has_no_contract():
    result, resident = await inspect({}, driver="alphaess")
    assert not result.validated
    assert result.reason == "no validated hold surface (#196)"
    assert resident["charge_current_a"] == "unknown"


async def test_resident_readback_and_independent_unknowns():
    values = identity() | {
        "sensor.solis_control_timed_charge_start_hours": 1,
        "sensor.solis_control_timed_charge_start_minutes": 15,
        "sensor.solis_control_timed_charge_end_hours": 4,
        "sensor.solis_control_timed_charge_end_minutes": 45,
        "sensor.solis_control_timed_charge_current": 12.3,
        "select.power": "Off",
    }
    _, resident = await inspect(values)
    assert resident == {"charge_window": "01:15–04:45", "charge_current_a": 12.3,
                        "power_switch": "off"}
    values["sensor.solis_control_timed_charge_start_minutes"] = "unavailable"
    values["sensor.solis_control_timed_charge_current"] = "NaN"
    values["select.power"] = "unavailable"
    _, resident = await inspect(values)
    assert set(resident.values()) == {"unknown"}


@pytest.mark.parametrize("value", ["", "reference", 2, -1])
def test_confirmation_bounds(value):
    with pytest.raises(ValidationError):
        Settings(solis_model_confirmed=value)


async def test_cli_read_only(tmp_path, monkeypatch, capsys):
    from ha_spark import cli
    settings = Settings(ha_url="http://ha.test", ha_token="fixture", data_dir=str(tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_authority", lambda *_: pytest.fail("authority accessed"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
        assert await cli._cmd_inverter_show(settings) == 0
        assert all(call.request.method == "GET" for call in mock.calls)
    output = json.loads(capsys.readouterr().out)
    assert not output[0]["validated"]
    assert list(tmp_path.iterdir()) == []


def test_cli_entry_point_is_read_only(tmp_path, monkeypatch, capsys):
    from ha_spark import cli
    settings = Settings(ha_url="http://ha.test", ha_token="fixture", data_dir=str(tmp_path))
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "load_authority", lambda *_: pytest.fail("authority accessed"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"http://ha.test/api/states/.*").respond(404)
        assert cli.main(["inverter", "show"]) == 0
        assert mock.calls
        assert all(call.request.method == "GET" for call in mock.calls)
    assert json.loads(capsys.readouterr().out)[0]["resident"]["charge_window"] == "unknown"
    assert list(tmp_path.iterdir()) == []


def test_overlay_identity_fixture():
    from pathlib import Path

    import yaml

    overlay = yaml.safe_load(Path("docs/solis-control-modbus-overlay.yaml").read_text())
    sensors = {sensor["address"]: sensor for sensor in overlay["modbus"][0]["sensors"]}
    for address, suffix in ((33000, "Model Word"), (35000, "Inverter Type Definition"),
                            (33289, "TOU Version")):
        sensor = sensors[address]
        assert sensor["name"] == f"Solis Control {suffix}"
        assert sensor["input_type"] == "input"
        assert sensor["data_type"] == "uint16"
        assert sensor["slave"] == 1
        assert sensor["scan_interval"] == 3600
        assert "scale" not in sensor


async def test_transport_error_reads_as_unknown():
    settings = Settings(ha_url="http://ha.test", ha_token="fixture",
                        inverter_power_switch_entity="select.power")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r"http://ha.test/api/states/.*").mock(
            side_effect=httpx.ConnectError("down"))
        async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
            device = inverter_device(settings, rest)
            validation = await device.hold_validation()
            resident = await device.resident_diagnostic()
    assert not validation.validated
    assert set(resident.values()) == {"unknown"}
