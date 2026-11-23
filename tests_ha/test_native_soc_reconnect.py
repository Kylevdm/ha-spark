"""#765: the first battery frames after a reconnect may carry a default 100 % SoC."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.native_entities import BankSoC
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def _reconnect(native_transport: FakeSolisDevice, c: Any, epoch: int) -> None:
    native_transport.connection_epoch = epoch
    await c.native.async_check_connections()


async def _poll_battery_frame(c: Any, freezer: FrozenDateTimeFactory) -> None:
    inverter = c.native.inverters["inv1"]
    before = c.native.readings.get(inverter.id, {}).get("input_33132")
    for _ in range(20):
        freezer.tick(timedelta(seconds=1))
        await c.native.async_poll(dt_util.utcnow())
        if c.native.readings[inverter.id].get("input_33132") is not before:
            return
    raise AssertionError("battery frame was not polled")


async def test_default_soc_after_a_reconnect_is_never_published(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [51])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    inverter = c.native.inverters["inv1"]
    bank_id = inverter.bank_id
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert c.soc.trusted(bank_id)
    assert BankSoC(c, bank_id).native_value == 51

    # Reconnect: the device's first battery frame carries a default 100 %.
    native_transport.set_input(33139, [100])
    await _reconnect(native_transport, c, 1)
    await _poll_battery_frame(c, freezer)
    assert BankSoC(c, bank_id).native_value != 100
    view = c.soc.bank(bank_id)
    assert view.value != 100 and not c.soc.trusted(bank_id)


async def test_default_soc_on_consecutive_reconnects_is_never_trusted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [51])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    bank_id = c.native.inverters["inv1"].bank_id
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert c.soc.trusted(bank_id)

    # Two reconnects in consecutive minutes, each observed on its default frame:
    # a repeated value must not be taken as a BMS recalibration.
    for epoch in (1, 2):
        native_transport.set_input(33139, [100])
        await _reconnect(native_transport, c, epoch)
        await _poll_battery_frame(c, freezer)
        freezer.tick(timedelta(minutes=1))
        c.soc.observe(dt_util.utcnow())
        native_transport.set_input(33139, [51])
    assert not (c.soc.trusted(bank_id) and c.soc.bank(bank_id).value == 100)


async def test_default_soc_in_the_first_frame_after_start_is_never_trusted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    # No baseline yet: the first observed value has nothing to be checked against.
    native_transport.set_read_frames()
    native_transport.set_input(33139, [100])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    bank_id = c.native.inverters["inv1"].bank_id
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert not (c.soc.trusted(bank_id) and c.soc.bank(bank_id).value == 100)
