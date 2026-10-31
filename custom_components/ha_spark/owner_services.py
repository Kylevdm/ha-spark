"""Exit-only owner services; caller names and service payloads are never logged."""
import logging
from datetime import time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import probatio as vol
from homeassistant.components import logbook, persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .authority_control import AuthorityControl
from .const import DOMAIN
from .transitions import TransitionRequest

if TYPE_CHECKING:
    from . import SparkConfigEntry

_LOGGER = logging.getLogger(__name__)
SERVICES = ("set_observe", "start_manual_control", "resume_control", "reprobe",
            "commands_pending")


def async_register(hass: HomeAssistant) -> None:
    """Register once for the site; no escalation or expected-epoch service field."""
    async def handle(call: ServiceCall) -> ServiceResponse:
        if call.service == "commands_pending":
            for loaded_entry in hass.config_entries.async_loaded_entries(DOMAIN):
                loaded_entry.runtime_data.async_commands_pending()
            return {}
        user = (await hass.auth.async_get_user(call.context.user_id)
                if call.context.user_id else None)
        if user is None or not user.is_admin or user.system_generated:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_owner")
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if len(entries) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="authority_unknown",
            )
        entry: SparkConfigEntry = entries[0]
        if call.service == "reprobe":
            if not await entry.runtime_data.async_reprobe(call.data["inverter_id"]):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="native_device_missing",
                )
            return {}
        control = entry.runtime_data.control
        target = {"set_observe": "observe", "start_manual_control": "manual",
                  "resume_control": "resume"}[call.service]
        request = TransitionRequest.model_validate({
            "target": target, "requester_kind": "service", **call.data,
        })
        at = _manual_time(entry)
        zone = control.household_zone()
        if zone is None and target == "manual" and request.duration == "until_tomorrow":
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_household_zone",
            )
        decision = await control.async_request(request, now=dt_util.utcnow(),
                                               zone=zone or ZoneInfo("UTC"), at=at)
        if decision.refusal is not None:
            reason = decision.refusal if decision.refusal in (
                "not_owner", "authority_unknown", "transition_in_progress",
                "no_manual_session", "not_in_active_mode",
            ) else "transition_in_progress"
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key=reason)
        state = decision.state
        if state is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="authority_unknown",
            )
        _LOGGER.info("Owner %s: %s epoch %s", user.id, target, state.epoch)
        logbook.async_log_entry(hass, "ha-spark", f"{user.id}: {target}, epoch {state.epoch}",
                          domain=DOMAIN, context=call.context)
        manual = state.manual_session
        return {"epoch": state.epoch, "deadline": (
            manual.deadline.isoformat() if manual is not None and manual.deadline else None),
        }

    for service in SERVICES:
        schema = vol.Schema({})
        if service == "reprobe":
            schema = vol.Schema({vol.Required("inverter_id"): str})
        if service == "start_manual_control":
            schema = vol.Schema({
                vol.Optional("duration", default="one_hour"): vol.In(
                    ("one_hour", "until_tomorrow", "until_resumed")),
                vol.Optional("keep_protection", default=True): bool,
            })
        hass.services.async_register(DOMAIN, service, handle, schema=schema,
                                     supports_response=SupportsResponse.OPTIONAL)


def _manual_time(entry: "SparkConfigEntry") -> time:
    raw = entry.options.get("manual_until_time", "07:00")
    try:
        if isinstance(raw, str):
            parsed = time.fromisoformat(raw)
            if parsed.tzinfo is None:
                return parsed
    except ValueError:
        pass
    return time(7)


async def async_handover(
    hass: HomeAssistant, entry: "SparkConfigEntry", control: AuthorityControl | None = None,
) -> None:
    """One removal attempt, not a reload/shutdown action. Failures remain visible."""
    control = control if control is not None else entry.runtime_data.control
    if not control.owned():
        return
    state = control.store.state
    if state is not None and state.committed_mode == "observe":
        return
    # Observe has no household-clock deadline. An invalid zone must not prevent
    # a de-escalating handover; it never grants any new charging authority.
    decision = await control.async_request(
        TransitionRequest(target="observe"), now=dt_util.utcnow(),
        zone=control.household_zone() or ZoneInfo("UTC"), removal=True,
    )
    if (decision.refusal is not None or decision.state is None
            or decision.state.committed_mode != "observe" or decision.state.transition is not None):
        persistent_notification.async_create(
            hass, "The verified Observe handover did not complete. Protection may remain active.",
            title="ha-spark handover incomplete",
            notification_id=f"ha_spark_handover_{entry.entry_id}",
        )
