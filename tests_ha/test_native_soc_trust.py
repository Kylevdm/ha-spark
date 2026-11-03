"""Native SoC trust through config entries, fake frames, options and v4 snapshots."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import FallbackState
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.native_entities import BankSocTrust
from custom_components.ha_spark.soc_trust import Policy
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry
from tests_ha.test_native_entities import ticks
from tests_ha.test_polling import fixture


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


async def test_native_soc_snapshot_preconditions_live_fences_and_no_writes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [73])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    bank_id = c.soc.topology.banks[0].id
    assert not c.soc.trusted(bank_id)
    assert not c.soc.trusted("missing")
    assert not c.soc.increases_allowed(bank_id)
    assert c.soc.policy == Policy()
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    before = len(native_transport.traffic)
    c.soc.observe(dt_util.utcnow())
    assert len(native_transport.traffic) == before
    assert c.soc.trusted(bank_id)
    assert c.soc.increases_allowed(bank_id)
    assert c.control.preconditions().soc_trusted
    assert c.control.preconditions().soc_recovered
    snapshot: dict[str, object] = {"api_version": 4}
    c._ownership_snapshot(snapshot)
    assert snapshot["banks"] == [{"id": bank_id, "soc": {
        "value": 73.0, "read_at": c.native.reading(
            c.native.inverters["inv1"], "input_33132").read_at.isoformat(),
        "trust": "trusted", "reason": "ok",
    }}]
    # The same v4 bank-SoC keys consumed by the other half's golden contract.
    reference = fixture("site_snapshot_v4_max")["banks"][0]["soc"]
    assert set(c.soc.snapshot()[0]["soc"]) == set(reference)
    sensor = BankSocTrust(c, bank_id)
    assert sensor.native_value == "trusted"
    assert sensor.extra_state_attributes == {"reason": "ok"}
    registry = er.async_get(hass)
    assert sensor.unique_id is not None
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, sensor.unique_id)
    assert entity_id is not None
    registered = registry.async_get(entity_id)
    assert registered is not None and registered.entity_category == "diagnostic"
    # Same minute: lost validation narrows immediately and counts once that minute.
    native_transport.connected = False
    assert not c.soc.trusted(bank_id)
    assert not c.soc.increases_allowed(bank_id)
    assert not c.control.preconditions().soc_trusted
    assert sensor.native_value == "untrusted"
    c.soc.observe(dt_util.utcnow())
    for _ in range(3):
        freezer.tick(timedelta(minutes=1))
        c.soc.observe(dt_util.utcnow())
    assert c.soc.fallback_requested
    assert all(t.op == "read" for t in native_transport.traffic)


async def test_failed_frame_ages_last_good_read_but_reconnect_never_reuses_it(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [50])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    bank_id = c.soc.topology.banks[0].id
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert c.soc.trusted(bank_id)
    native_transport.input.pop(33139)
    # Poll all due frames until the battery frame fails, without new SoC evidence.
    for _ in range(10):
        freezer.tick(timedelta(seconds=1))
        await c.native.async_poll(dt_util.utcnow())
    assert c.native.reading(c.native.inverters["inv1"], "input_33132") is None
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert c.soc.trusted(bank_id)
    freezer.tick(timedelta(minutes=10))
    c.soc.observe(dt_util.utcnow())
    assert c.soc.bank(bank_id).reason == "stale"
    native_transport.connection_epoch = 1
    assert not c.soc.trusted(bank_id)
    assert all(t.op == "read" for t in native_transport.traffic)


async def test_shared_bank_and_reload_start_untrusted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    entry = await native_entry(hass, aioclient_mock, two_units=True)
    c = entry.runtime_data
    bank_id = c.soc.topology.banks[0].id
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    assert not c.soc.trusted(bank_id)
    assert c.soc.snapshot() == [{"id": bank_id, "soc": None}]
    for _ in range(4):  # never trusted, but never counted toward fallback either
        freezer.tick(timedelta(minutes=1))
        c.soc.observe(dt_util.utcnow())
    assert not c.soc.fallback_requested
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.soc.bank(bank_id).trust == "untrusted"
    assert not entry.runtime_data.soc.fallback_requested


async def test_durable_soc_cause_restarts_recovery_without_persisted_progress(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [50])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={"fallback": FallbackState(
        active=True, causes=("soc_integrity",), entered_at=dt_util.utcnow(),
    )}))
    await c.native.async_poll(dt_util.utcnow())
    freezer.tick(timedelta(minutes=1))
    c.soc.observe(dt_util.utcnow())
    bank_id = c.soc.topology.banks[0].id
    assert c.soc.bank(bank_id).trust == "recovering"
    assert not c.soc.trusted(bank_id)
    assert not c.soc.recovery_ready
    assert not c.control.preconditions().soc_recovered
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.soc.bank(bank_id).trust == "untrusted"
    assert not entry.runtime_data.soc.recovery_ready


@pytest.mark.parametrize("field,value", [
    ("soc_max_report_age_minutes", 0), ("soc_max_report_age_minutes", float("inf")),
    ("soc_failure_threshold", 0), ("soc_failure_threshold", 1.5),
    ("soc_recovery_minutes", 0),
])
async def test_soc_policy_rejects_invalid_input(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    field: str, value: float,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "soc"})
    assert result["step_id"] == "soc"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**Policy().model_dump(), field: value},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_soc_policy"}
    assert field not in entry.options


async def test_soc_policy_save_preserves_options_and_reloads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    entry = await native_entry(hass, aioclient_mock)
    before = dict(entry.options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                               {"next_step_id": "soc"})
    policy = {"soc_max_report_age_minutes": 5.5, "soc_failure_threshold": 2,
              "soc_recovery_minutes": 3}
    result = await hass.config_entries.options.async_configure(result["flow_id"], policy)
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert dict(entry.options) == {**before, **policy}
    assert entry.runtime_data.soc.policy == Policy.model_validate(policy)


async def test_timer_observes_native_frames_and_invalid_soc_masks_trust_immediately(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
) -> None:
    native_transport.set_read_frames()
    native_transport.set_input(33139, [50])
    entry = await native_entry(hass, aioclient_mock)
    c = entry.runtime_data
    bank_id = c.soc.topology.banks[0].id
    await ticks(hass, freezer, 61)
    assert c.soc.trusted(bank_id)
    native_transport.set_input(33139, [101])
    await ticks(hass, freezer, 10)
    assert not c.soc.trusted(bank_id)
    assert not c.soc.increases_allowed(bank_id)
    # Raw/forged HA state is never used as a replacement for the invalid register.
    hass.states.async_set("sensor.battery_bank_soc", "50")
    assert not c.soc.trusted(bank_id)
    assert all(t.op == "read" for t in native_transport.traffic)
