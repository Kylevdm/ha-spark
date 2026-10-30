"""Regression coverage for handover obligations and restart ordering."""
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import ManualSession, Transition
from custom_components.ha_spark.owner_services import async_handover
from custom_components.ha_spark.transitions import (
    Preconditions,
    TransitionRequest,
    propose_transition,
)
from tests_ha.test_native_transitions import state
from tests_ha.test_owner_control import NOW, ZONE, Writer, frozen, hass_config_dir, hass_storage
from tests_ha.test_polling import URL, setup

# Reuse isolated storage and the frozen UTC clock, not production connections.
__all__ = ["frozen", "hass_config_dir", "hass_storage"]


def test_repeated_observe_preserves_progress_failures_and_epoch() -> None:
    pending = state("optimise").model_copy(update={"transition": Transition(
        target="observe", started_at=NOW, steps=("release_holds",),
        failing_actions=("release_holds",)),
    })
    decision = propose_transition(pending, TransitionRequest(target="observe"),
                                  Preconditions(), now=NOW + timedelta(minutes=1))
    assert decision.state == pending


@pytest.mark.parametrize("removal", [False, True])
@pytest.mark.parametrize("during_grace", [False, True])
async def test_observe_keeps_pending_manual_export_cleanup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, removal: bool, during_grace: bool,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    assert await control.store.async_initialise()
    state = control.store.state
    assert state is not None
    assert await control.store.async_save(state.model_copy(update={"committed_mode": "optimise"}))
    writer = Writer()
    control.writer = writer
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_start(now=NOW, zone=ZONE)
        control.grace_until = NOW
        writer.fail = "cancel_export"
        await control.async_request(TransitionRequest(target="manual"), now=NOW, zone=ZONE)
        writer.effects.clear()
        if during_grace:
            control.grace_until = NOW + timedelta(minutes=2)
        if removal:
            await async_handover(hass, entry)
        else:
            await control.async_request(TransitionRequest(target="observe"), now=NOW, zone=ZONE)
        assert writer.effects == ([] if during_grace else ["cancel_export"])
        state = control.store.state
        assert state is not None and state.committed_mode == "optimise"
        assert state.transition is not None
        assert state.transition.steps == ("cancel_export", "release_holds")
        writer.fail = None
        control.grace_until = NOW
        writer.effects.clear()
        await control.async_retry()
        assert writer.effects == ["cancel_export", "release_holds"]
        assert control.store.state is not None
        assert control.store.state.committed_mode == "observe"


async def test_return_to_only_and_repeated_observe_preserve_pending_work(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    assert await control.store.async_initialise()
    state = control.store.state
    assert state is not None
    assert await control.store.async_save(state.model_copy(update={
        "committed_mode": "optimise", "manual_session": ManualSession(return_to="optimise"),
        "transition": Transition(target="optimise", started_at=NOW, steps=("cancel_export",),
                                 failing_actions=("cancel_export",)),
    }))
    writer = Writer()
    control.writer = writer
    control.ready = True
    control.grace_until = NOW
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_request(TransitionRequest(target="protect"), now=NOW, zone=ZONE)
        assert not writer.effects
        writer.fail = "cancel_export"
        await control.async_request(TransitionRequest(target="observe"), now=NOW, zone=ZONE)
        pending = control.store.state
        writer.effects.clear()
        await control.async_request(TransitionRequest(target="observe"), now=NOW, zone=ZONE)
        assert control.store.state == pending
        assert not writer.effects


@pytest.mark.parametrize("failed_stage", [
    None, "absent", "connect", "read_device_state", "read_evidence", "reconcile_holds",
])
async def test_expiry_waits_for_startup_and_grace_starts_after_slow_reads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory, failed_stage: str | None,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    assert await control.store.async_initialise()
    state = control.store.state
    assert state is not None
    assert await control.store.async_save(state.model_copy(update={
        "committed_mode": "optimise", "manual_session": ManualSession(
            return_to="optimise", deadline=NOW + timedelta(minutes=1)),
    }))

    class SlowWriter(Writer):
        async def async_startup(self, stage: str) -> bool:
            self.effects.append(stage)
            freezer.tick(timedelta(minutes=1))
            return stage != failed_stage

    writer = SlowWriter()
    control.writer = None if failed_stage == "absent" else writer
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_start(now=NOW, zone=ZONE)
        await control.async_expire(now=NOW + timedelta(minutes=4), zone=ZONE)
        state = control.store.state
        assert state is not None
        if failed_stage is not None:
            assert state.manual_session is not None and state.epoch == 1
            assert not control.ready
        else:
            assert state.manual_session is None and state.epoch == 2
            assert control.grace_until == NOW + timedelta(minutes=6)
            assert not control.permits_non_protection(NOW + timedelta(minutes=5, seconds=59))
            assert not any(effect in writer.effects for effect in (
                "verify_fresh_plan", "write_fallback"))


@pytest.mark.parametrize("service", ["set_observe", "resume_control", "start_manual_control"])
async def test_bad_zone_does_not_block_zone_independent_owner_services(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_admin_user: Any, service: str,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    hass.config_entries.async_update_entry(entry, options={"household_time_zone": "Not/AZone"})
    control = entry.runtime_data.control
    assert await control.store.async_initialise()
    state = control.store.state
    assert state is not None
    assert await control.store.async_save(state.model_copy(update={
        "committed_mode": "protect", "manual_session": (
            None if service == "start_manual_control" else ManualSession(return_to="protect")),
    }))
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await hass.services.async_call("ha_spark", service, {}, blocking=True,
                                      context=Context(user_id=hass_admin_user.id))
        if service == "start_manual_control":
            await control.async_request(TransitionRequest(target="observe"), now=NOW, zone=ZONE)
            with pytest.raises(ServiceValidationError) as error:
                await hass.services.async_call(
                    "ha_spark", service, {"duration": "until_tomorrow"}, blocking=True,
                    context=Context(user_id=hass_admin_user.id))
            assert error.value.translation_key == "invalid_household_zone"
