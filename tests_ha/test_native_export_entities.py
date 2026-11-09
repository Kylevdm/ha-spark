"""Native export diagnostic entities (#599)."""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import (
    AuthorityState,
    EventIdentity,
    ExportRecord,
    ExportSupervision,
    ExportWindow,
    GatewayClaim,
)
from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.native import Probe
from custom_components.ha_spark.native_export_entities import (
    ExportBoundSensor,
    ExportRecordSensor,
    ExportSupervisionSensor,
    native_export_sensors,
)
from custom_components.ha_spark.topology import Inverter
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_device import native_entry


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def _state(c: SparkCoordinator) -> AuthorityState:
    state = c.authority.state
    assert state is not None
    return state


@pytest.fixture
async def export_site(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
                      native_transport: FakeSolisDevice, freezer: FrozenDateTimeFactory,
                      ) -> SparkCoordinator:
    freezer.move_to("2026-11-02T12:00:00Z")
    entry = await native_entry(hass, aioclient_mock)
    c: SparkCoordinator = entry.runtime_data
    return c


def _claim(entry_options: Mapping[str, Any]) -> GatewayClaim:
    raw = entry_options["gateway_claims"][0]
    return GatewayClaim.model_validate(raw)


def _identity() -> tuple[int, int, int]:
    return (0x0026, 0x2031, 0)


def _first_inverter(c: SparkCoordinator) -> Inverter:
    return next(iter(c.native.inverters.values()))


async def test_native_export_sensors_returns_three_per_inverter(
    export_site: SparkCoordinator,
) -> None:
    """native_export_sensors creates bound, supervision and record sensors."""
    sensors = native_export_sensors(export_site)
    count = len(export_site.native.inverters)
    assert len(sensors) == count * 3
    assert sum(1 for s in sensors if isinstance(s, ExportBoundSensor)) == count
    assert sum(1 for s in sensors if isinstance(s, ExportSupervisionSensor)) == count
    assert sum(1 for s in sensors if isinstance(s, ExportRecordSensor)) == count


async def test_bound_sensor_returns_latest_supervision_rating(
    export_site: SparkCoordinator,
) -> None:
    """ExportBoundSensor.native_value reports the inverter's latest supervision."""
    c = export_site
    inverter = _first_inverter(c)
    claim = _claim(c.entry.options)
    identity = _identity()
    c.native.probes[inverter.id] = Probe(reason=None, identity=identity)
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=2))
    supervision = ExportSupervision(
        inverter_id=inverter.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=1000.0, identity=identity, claim=claim)
    await c.authority.async_save(_state(c).model_copy(update={
        "export_supervisions": (supervision,)}))
    sensor = ExportBoundSensor(c, inverter, "native_export_bound")
    assert sensor.native_value == 1000.0


async def test_bound_sensor_without_supervision_returns_none(
    export_site: SparkCoordinator,
) -> None:
    """ExportBoundSensor.native_value is None when no supervision exists."""
    c = export_site
    inverter = _first_inverter(c)
    sensor = ExportBoundSensor(c, inverter, "native_export_bound")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


async def test_bound_sensor_attributes_validated_when_bound_holds(
    export_site: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching claim/identity and in-limit rating is reported validated."""
    c = export_site
    inverter = _first_inverter(c)
    claim = _claim(c.entry.options)
    identity = _identity()
    monkeypatch.setattr(c.native, "reason", lambda inv: None)
    c.native.probes[inverter.id] = Probe(reason=None, caching=False, identity=identity)
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=2))
    supervision = ExportSupervision(
        inverter_id=inverter.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=1000.0, identity=identity, claim=claim)
    await c.authority.async_save(_state(c).model_copy(update={
        "export_supervisions": (supervision,)}))
    sensor = ExportBoundSensor(c, inverter, "native_export_bound")
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["validated"] is True
    assert attrs["reason"] is None


async def test_bound_sensor_attributes_unverified_for_bad_topology(
    export_site: SparkCoordinator,
) -> None:
    """An invalid topology makes every bound unverified."""
    c = export_site
    inverter = _first_inverter(c)
    claim = _claim(c.entry.options)
    identity = _identity()
    c.native.probes[inverter.id] = Probe(reason=None, identity=identity)
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=2))
    supervision = ExportSupervision(
        inverter_id=inverter.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=1000.0, identity=identity, claim=claim)
    await c.authority.async_save(_state(c).model_copy(update={
        "export_supervisions": (supervision,)}))
    object.__setattr__(c.entry, "options",
                       {**c.entry.options, "topology": {"connections": []}})
    sensor = ExportBoundSensor(c, inverter, "native_export_bound")
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["validated"] is False
    assert attrs["reason"] == "export_bound_unverified"


async def test_supervision_sensor_reads_end_and_event_attributes(
    export_site: SparkCoordinator,
) -> None:
    """ExportSupervisionSensor exposes the supervision window and id."""
    c = export_site
    inverter = _first_inverter(c)
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=5))
    supervision = ExportSupervision(
        inverter_id=inverter.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=1500.0,
        identity=_identity(), claim=_claim(c.entry.options))
    await c.authority.async_save(_state(c).model_copy(update={
        "export_supervisions": (supervision,)}))
    sensor = ExportSupervisionSensor(c, inverter, "native_export_supervision")
    assert sensor.native_value == event.end
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["supervision_id"] == str(supervision.id)
    assert attrs["event_start"] == event.start.isoformat()
    assert attrs["event_end"] == event.end.isoformat()


async def test_record_sensor_reads_state_and_window_attributes(
    export_site: SparkCoordinator,
) -> None:
    """ExportRecordSensor exposes record state, window and cleanup flags."""
    c = export_site
    inverter = _first_inverter(c)
    state = _state(c)
    now = dt_util.utcnow()
    window = ExportWindow(start=now, end=now + timedelta(hours=1))
    event = EventIdentity(direction="export", start=window.start, end=window.end)
    record = ExportRecord(
        inverter_id=inverter.id, event=event, window=window,
        store_id=state.store_id, epoch=state.epoch,
        supervision_id=uuid4(), state="armed",
        last_reconcile_at=now, cleanup_verified=True,
        stop_reason="user_cancelled")
    await c.authority.async_save(state.model_copy(update={
        "export_records": (record,)}))
    sensor = ExportRecordSensor(c, inverter, "native_export_record")
    assert sensor.native_value == "armed"
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["window_start"] == window.start.isoformat()
    assert attrs["window_end"] == window.end.isoformat()
    assert attrs["cleanup_verified"] is True
    assert attrs["stop_reason"] == "user_cancelled"


async def test_export_entity_available_follows_authority_state(
    export_site: SparkCoordinator,
) -> None:
    """ExportEntity.available is True exactly when an authority state exists."""
    c = export_site
    inverter = _first_inverter(c)
    sensor = ExportBoundSensor(c, inverter, "native_export_bound")
    assert sensor.available is True
    real_state = c.authority._state
    try:
        c.authority._state = None
        assert sensor.available is False
    finally:
        c.authority._state = real_state


async def test_supervision_per_inverter_not_cross_contaminated(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    """The latest supervision is selected by inverter id, not global order."""
    freezer.move_to("2026-11-02T12:00:00Z")
    c: SparkCoordinator = (await native_entry(hass, aioclient_mock, two_units=True)).runtime_data
    inverters = list(c.native.inverters.values())
    assert len(inverters) == 2
    first, second = inverters[0], inverters[1]
    now = dt_util.utcnow()
    event = EventIdentity(direction="export", start=now + timedelta(minutes=1),
                          end=now + timedelta(minutes=2))
    sup_first = ExportSupervision(
        inverter_id=first.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=1111.0,
        identity=_identity(), claim=_claim(c.entry.options))
    sup_second = ExportSupervision(
        inverter_id=second.id, id=uuid4(), event=event,
        confirming_user_id="user", epoch=_state(c).epoch,
        ends_at=event.end, rating_w=2222.0,
        identity=_identity(), claim=_claim(c.entry.options))
    await c.authority.async_save(_state(c).model_copy(update={
        "export_supervisions": (sup_first, sup_second)}))
    assert ExportBoundSensor(c, first, "native_export_bound").native_value == 1111.0
    assert ExportBoundSensor(c, second, "native_export_bound").native_value == 2222.0
