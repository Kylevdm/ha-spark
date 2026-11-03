"""Verified transition, startup and owner-service effects at the config-entry seam."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_spark.authority_store import AuthorityState, FallbackState, ManualSession
from custom_components.ha_spark.owner_services import async_handover
from custom_components.ha_spark.transitions import Preconditions, TransitionRequest
from tests_ha.test_polling import URL, setup

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
ZONE = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


class Writer:
    def __init__(self) -> None:
        self.effects: list[str] = []
        self.fail: str | None = None

    async def async_barrier(self) -> None:
        pass

    async def async_startup(self, stage: str) -> bool:
        self.effects.append(stage)
        return True

    async def async_step(self, step: str, state: AuthorityState, *, removal: bool) -> bool:
        self.effects.append(step)
        return step != self.fail


async def test_activation_partial_failure_keeps_grant_and_retries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    control = entry.runtime_data.control
    assert await store.async_initialise()
    writer = Writer()
    control.writer = writer
    control.preconditions = lambda: Preconditions(True, True, True, True, True, True, True)
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_start(now=NOW, zone=ZONE)
        freezer.tick(timedelta(minutes=2))
        assert writer.effects == [
            "connect", "read_device_state", "read_evidence",  # Observe never reconciles writes.
        ]
        writer.fail = "verify_fresh_plan"
        decision = await control.async_request(TransitionRequest(target="optimise"), now=NOW,
                                               zone=ZONE)
        assert decision.state is not None and decision.state.committed_mode == "optimise"
        assert decision.state.transition is not None
        assert decision.state.transition.failing_actions == ("verify_fresh_plan",)
        assert decision.state.epoch == 2
        writer.fail = None
        await control.async_retry()
        assert store.state is not None and store.state.transition is None
        assert store.state.committed_mode == "optimise" and store.state.epoch == 2
        writer.effects.clear()
        await control.async_request(TransitionRequest(target="protect"), now=NOW, zone=ZONE)
        assert writer.effects == ["cancel_export", "write_fallback"]
        assert store.state is not None and store.state.committed_mode == "protect"
        writer.effects.clear()
        writer.fail = "release_holds"
        await control.async_request(TransitionRequest(target="observe"), now=NOW, zone=ZONE)
        assert writer.effects == ["release_holds"]
        assert store.state is not None and store.state.committed_mode == "protect"
        assert store.state.transition is not None
        assert store.state.transition.failing_actions == ("release_holds",)
        writer.fail = None
        await control.async_retry()
        assert store.state is not None and store.state.committed_mode == "observe"
        writer.effects.clear()
        await control.async_retry()
        assert not writer.effects


async def test_refusals_and_failed_save_never_write(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    writer = Writer()
    control.writer = writer
    decision = await control.async_request(
        TransitionRequest(target="observe"), now=NOW, zone=ZONE,
    )
    assert decision.refusal == "not_owner" and not writer.effects
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        decision = await control.async_request(
            TransitionRequest(target="observe"), now=NOW, zone=ZONE,
        )
        assert decision.refusal == "authority_unknown"
        assert await control.store.async_initialise()
        decision = await control.async_request(
            TransitionRequest(target="optimise"), now=NOW, zone=ZONE,
        )
        assert decision.refusal == "preconditions_unmet" and len(decision.reasons) == 7
        assert not writer.effects
        control.preconditions = lambda: Preconditions(True, True, True)
        with patch.object(control.store, "async_save", return_value=False):
            decision = await control.async_request(TransitionRequest(target="protect"),
                                                   now=NOW, zone=ZONE)
        assert decision.refusal == "authority_unknown"
        assert not writer.effects


async def test_manual_expiry_startup_grace_and_durable_session_epoch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    store = control.store
    assert await store.async_initialise()
    assert store.state is not None
    assert await store.async_save(store.state.model_copy(update={
        "committed_mode": "optimise", "manual_session": ManualSession(
            return_to="optimise", deadline=NOW - timedelta(seconds=1)),
    }))
    writer = Writer()
    control.writer = writer
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_start(now=NOW, zone=ZONE)
        assert store.state is not None and store.state.manual_session is None
        assert store.state.transition is not None
        assert store.state.transition.steps == ("verify_fresh_plan",)
        assert writer.effects == [
            "connect", "read_device_state", "read_evidence", "reconcile_holds",
        ]
        assert not control.permits_non_protection(NOW + timedelta(seconds=119))
        assert control.permits_non_protection(NOW + timedelta(seconds=120))
        epoch = store.state.epoch
        assert await control.async_new_session("session", now=NOW)
        await store.async_load()
        assert await control.async_new_session("session", now=NOW)
        assert store.state is not None and store.state.epoch == epoch + 1
        assert await control.async_fallback(FallbackState(active=True, causes=("soc_integrity",),
                                                          entered_at=NOW), now=NOW)
        assert store.state is not None and store.state.epoch == epoch + 2


async def test_owner_services_admin_only_and_audited(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_admin_user: Any,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()
    assert store.state is not None
    assert await store.async_save(store.state.model_copy(update={"committed_mode": "protect"}))
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call("ha_spark", "start_manual_control", {}, blocking=True)
    assert error.value.translation_key == "not_owner"
    with (
        patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"),
        patch("custom_components.ha_spark.owner_services.logbook.async_log_entry") as log,
    ):
        response = await hass.services.async_call(
            "ha_spark", "start_manual_control", {}, blocking=True, return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
        assert response == {"epoch": 2, "deadline": "2026-10-30T13:00:00+00:00"}
        assert log.call_count == 1
        assert store.state is not None and store.state.manual_session is not None
        assert store.state.manual_session.keep_protection
    assert not hass.services.has_service("ha_spark", "set_optimise")
    assert not hass.services.has_service("ha_spark", "set_protect")


async def test_real_entry_removal_attempts_verified_release_after_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    control = entry.runtime_data.control
    store = control.store
    assert await store.async_initialise()
    assert store.state is not None
    assert await store.async_save(store.state.model_copy(update={"committed_mode": "protect"}))
    writer = Writer()
    control.writer = writer
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await control.async_start(now=NOW - timedelta(minutes=3), zone=ZONE)
        writer.effects.clear()
        await hass.config_entries.async_remove(entry.entry_id)
    assert writer.effects == ["release_holds"]
    assert store.state is not None and store.state.committed_mode == "observe"
    assert store.state.transition is None


@pytest.mark.parametrize(("admin", "system"), [(False, False), (True, True)])
async def test_owner_services_refuse_non_admin_and_system_users(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, admin: bool, system: bool,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    await setup(hass)
    user = SimpleNamespace(is_admin=admin, system_generated=system, id="user-id")
    with patch.object(hass.auth, "async_get_user", return_value=user):
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(
                "ha_spark", "set_observe", {}, blocking=True, context=Context(user_id="user-id"),
            )
    assert error.value.translation_key == "not_owner"


async def test_removal_reports_unverified_handover(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    store = entry.runtime_data.authority
    assert await store.async_initialise()
    assert store.state is not None
    assert await store.async_save(store.state.model_copy(update={"committed_mode": "protect"}))
    with (
        patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"),
        patch(
            "custom_components.ha_spark.owner_services.persistent_notification.async_create",
        ) as n,
    ):
        await async_handover(hass, entry)
        assert n.call_count == 1
        assert store.state is not None and store.state.committed_mode == "protect"
