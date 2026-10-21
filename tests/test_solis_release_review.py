"""#449: a verified safe-state On re-arms only its own device's refused release."""
from datetime import UTC, datetime, timedelta

from ha_spark.authority import HoldDevice, HoldRecord
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext
from ha_spark.devices.inverters.solis import SolisDevice
from tests.authority_helpers import seed_authority
from tests.test_solis_power_switch import FakeRest


async def test_verified_safe_state_resets_release_refused_notice(tmp_path):
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="Europe/London",
                        db_path=str(tmp_path / "ledger.db"),
                        inverter_power_switch_entity="select.solisac_power_switch")
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", context=context)
    now = datetime.now(UTC)
    store.save_holds((HoldRecord(
        cause="ev", started_at=now - timedelta(hours=1), confirmed=True,
        device=HoldDevice(driver="solis", power_switch=settings.inverter_power_switch_entity,
                          hub=settings.solis_control_hub, slave=settings.solis_modbus_slave),
        release_refused_since=now, release_refused_reason="switch_unreadable",
        release_refused_notified=True,
    ),))
    rest = FakeRest(switch="Off")
    device = SolisDevice(settings.devices[0], settings, rest, execution=context)
    await device.write_safe_state()
    assert store.state.holds[0].release_refused_since is None
    assert store.state.holds[0].release_refused_notified is False


async def test_verified_safe_state_preserves_other_device_refusal(tmp_path):
    settings = Settings(ha_url="http://ha.test", ha_token="t", timezone="Europe/London",
                        db_path=str(tmp_path / "ledger.db"),
                        inverter_power_switch_entity="select.solisac_power_switch")
    first = settings.devices[0]
    settings = settings.model_copy(update={"devices": [first, first.model_copy(update={
        "entities": {**first.entities, "power_switch": "select.other_power_switch"},
    })]})
    context = ExecutionContext()
    store = seed_authority(settings, "optimise", context=context)
    now = datetime.now(UTC)
    record = HoldRecord(
        cause="ev", started_at=now - timedelta(hours=1), confirmed=True,
        device=HoldDevice(driver="solis", power_switch=settings.inverter_power_switch_entity,
                          hub=settings.solis_control_hub, slave=settings.solis_modbus_slave),
        release_refused_since=now, release_refused_reason="switch_unreadable",
        release_refused_notified=True,
    )
    other = record.model_copy(update={"device": record.device.model_copy(update={
        "power_switch": "select.other_power_switch",
    })})
    store.save_holds((record, other))
    rest = FakeRest(switch="Off")
    device = SolisDevice(settings.devices[0], settings, rest, execution=context)
    await device.write_safe_state()
    assert store.state.holds[0].release_refused_since is None
    assert store.state.holds[1] == other
