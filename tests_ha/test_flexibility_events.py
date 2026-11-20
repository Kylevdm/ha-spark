"""Flexibility events opt-in: the ratings options step and the admin-only export switch."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError

from custom_components.ha_spark.coordinator import SparkCoordinator
from custom_components.ha_spark.flexibility_events import OPTION_KEY, stored
from tests_ha.test_native_write import hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as current_writer
from tests_ha.test_topology_flow import STORED_TOPOLOGY, choose, entry_for

writer = current_writer
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
OFF: dict[str, Any] = {"export_during_events": False, "export_enabled_by": None,
       "export_enabled_at": None, "export_ratings_w": {}}


@pytest.fixture(autouse=True)
def clock(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


@pytest.fixture
def admin(writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writer.hass.auth, "async_get_user", AsyncMock(
        return_value=SimpleNamespace(is_admin=True, system_generated=False, id="user-id")))


async def open_step(c: SparkCoordinator) -> str:
    result = await c.hass.config_entries.options.async_init(c.entry.entry_id)
    assert "flexibility_events" in result["menu_options"]
    flow_id: str = result["flow_id"]
    result = await choose(c.hass, flow_id, "flexibility_events")
    assert result["type"].value == "form" and result["step_id"] == "flexibility_events"
    return flow_id


async def submit(c: SparkCoordinator, payload: dict[str, Any]) -> Any:
    flow_id = await open_step(c)
    with patch.object(c.hass.config_entries, "async_reload", return_value=True):
        result = await c.hass.config_entries.options.async_configure(flow_id, payload)
        await c.hass.async_block_till_done()
    return result


async def switch(c: SparkCoordinator, enabled: bool, user: str | None = "user-id") -> None:
    await c.hass.services.async_call(
        "ha_spark", "set_export_during_events", {"enabled": enabled}, blocking=True,
        context=Context(user_id=user))


def options(c: SparkCoordinator) -> Any:
    return c.entry.options.get(OPTION_KEY)


async def test_step_is_offered_only_on_native_sites(hass: Any) -> None:
    entry = entry_for(hass, topology=deepcopy(STORED_TOPOLOGY))
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "flexibility_events" not in result["menu_options"]


async def test_switch_is_off_by_default(writer: SparkCoordinator) -> None:
    assert options(writer) is None
    assert not stored(options(writer)).export_during_events


async def test_ratings_are_saved_when_the_export_checks_pass(writer: SparkCoordinator) -> None:
    result = await submit(writer, {"rating_inv1": 3000})
    assert result["type"].value == "create_entry"
    assert options(writer) == {**OFF, "export_ratings_w": {"inv1": 3000.0}}


async def test_ratings_above_the_site_export_limit_are_refused(
    writer: SparkCoordinator,
) -> None:
    result = await submit(writer, {"rating_inv1": 3601})
    assert result["errors"] == {"base": "export_bounds_exceed_site_limit"}
    assert options(writer) is None


async def test_ratings_without_a_verified_identity_are_refused(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(writer.native, "probes", {})
    result = await submit(writer, {"rating_inv1": 3000})
    assert result["errors"] == {"base": "export_bound_unverified"}
    assert options(writer) is None


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_every_native_inverter_needs_a_rating(writer: SparkCoordinator) -> None:
    result = await submit(writer, {"rating_inv1": 1000})
    assert result["errors"] == {"base": "missing_rating"}
    assert options(writer) is None


async def test_admin_turns_export_on_and_is_recorded(
    writer: SparkCoordinator, admin: None,
) -> None:
    await submit(writer, {"rating_inv1": 3000})
    await switch(writer, True)
    opt_in = stored(options(writer))
    assert opt_in.export_during_events and opt_in.export_enabled_by == "user-id"
    assert opt_in.export_enabled_at == NOW
    assert opt_in.export_ratings_w == {"inv1": 3000.0}


async def test_export_cannot_be_turned_on_without_ratings(
    writer: SparkCoordinator, admin: None,
) -> None:
    with pytest.raises(ServiceValidationError) as error:
        await switch(writer, True)
    assert error.value.translation_key == "missing_rating"
    assert not stored(options(writer)).export_during_events


async def test_export_on_rechecks_ratings_that_no_longer_pass(
    writer: SparkCoordinator, admin: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await submit(writer, {"rating_inv1": 3000})
    monkeypatch.setattr(writer.native, "probes", {})
    with pytest.raises(ServiceValidationError) as error:
        await switch(writer, True)
    assert error.value.translation_key == "export_bound_unverified"
    assert not stored(options(writer)).export_during_events


@pytest.mark.parametrize(("user", "admin_flag", "system"), [
    (None, True, False), ("user-id", False, False), ("user-id", True, True),
])
async def test_only_a_real_admin_can_switch_export(
    writer: SparkCoordinator, monkeypatch: pytest.MonkeyPatch,
    user: str | None, admin_flag: bool, system: bool,
) -> None:
    await submit(writer, {"rating_inv1": 3000})
    monkeypatch.setattr(writer.hass.auth, "async_get_user", AsyncMock(
        return_value=SimpleNamespace(is_admin=admin_flag, system_generated=system,
                                     id="user-id")))
    with pytest.raises(ServiceValidationError) as error:
        await switch(writer, True, user=user)
    assert error.value.translation_key == "not_owner"
    assert not stored(options(writer)).export_during_events


async def test_turning_export_off_keeps_ratings_and_clears_nothing(
    writer: SparkCoordinator, admin: None,
) -> None:
    await submit(writer, {"rating_inv1": 3000})
    await switch(writer, True)
    before = writer.authority.state
    await switch(writer, False)
    assert options(writer) == {**OFF, "export_ratings_w": {"inv1": 3000.0}}
    assert writer.authority.state == before


async def test_ratings_are_locked_while_export_is_on(
    writer: SparkCoordinator, admin: None,
) -> None:
    await submit(writer, {"rating_inv1": 3000})
    await switch(writer, True)
    result = await submit(writer, {"rating_inv1": 2000})
    assert result["errors"] == {"base": "export_on"}
    assert stored(options(writer)).export_ratings_w == {"inv1": 3000.0}


async def test_empty_submit_clears_ratings_while_off(writer: SparkCoordinator) -> None:
    await submit(writer, {"rating_inv1": 3000})
    result = await submit(writer, {})
    assert result["type"].value == "create_entry"
    assert options(writer) == OFF


@pytest.mark.parametrize("raw", [
    {"export_during_events": True, "export_ratings_w": {"inv1": 3000}},
    {"export_during_events": "yes"}, {"unexpected": 1}, "on",
])
def test_malformed_or_unaccountable_stored_switch_is_off(raw: object) -> None:
    assert not stored(raw).export_during_events
