"""Best-effort native notices, with persistent fallback and no control waits.

Producers own cadence and durable markers. This sink revalidates the current
option for each notice and runs bounded service delivery in an entry-owned task.
"""
import asyncio
import logging
import re

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

NOTIFY_TARGET_OPTION = "notify_target"
DELIVERY_TIMEOUT = 10
_PENDING = "ha_spark_notice_pending"
_LOGGER = logging.getLogger(__name__)


def valid_name(value: object) -> str | None:
    """Accept only a bare service name that needs no entity target."""
    if (isinstance(value, str) and re.fullmatch(r"[a-z0-9_]+", value)
            and value != "send_message"):
        return value
    return None


def _pending(hass: HomeAssistant) -> dict[str, object]:
    """In-flight deliveries by notification id; a dismiss withdraws the fallback."""
    pending: dict[str, object] = hass.data.setdefault(_PENDING, {})
    return pending


@callback
def async_notice(hass: HomeAssistant, entry: ConfigEntry, *, notification_id: str,
                 title: str, message: str) -> None:
    """Schedule phone delivery without waiting; unset targets behave as before."""
    name = valid_name(entry.options.get(NOTIFY_TARGET_OPTION))
    if name is None or not hass.services.has_service("notify", name):
        persistent_notification.async_create(
            hass, message, title=title, notification_id=notification_id)
        return
    token = object()
    _pending(hass)[notification_id] = token

    def fallback() -> None:
        # Only while this notice is still wanted: never revive a dismissed one.
        if _pending(hass).get(notification_id) is token:
            del _pending(hass)[notification_id]
            persistent_notification.async_create(
                hass, message, title=title, notification_id=notification_id)

    async def deliver() -> None:
        call = asyncio.ensure_future(hass.services.async_call(
            "notify", name, {"title": title, "message": message}, blocking=True))
        try:
            # The deadline is decided here, so a service that ignores
            # cancellation still cannot hide the notice.
            done, _ = await asyncio.wait({call}, timeout=DELIVERY_TIMEOUT)
        except asyncio.CancelledError:
            # An unload or reload mid-delivery must not lose the notice.
            call.cancel()
            fallback()
            raise
        if call in done and not call.cancelled() and call.exception() is None:
            if _pending(hass).get(notification_id) is token:
                del _pending(hass)[notification_id]
            return
        call.cancel()
        _LOGGER.warning("Notify target delivery failed; using a persistent notification")
        fallback()

    # Not eager: no part of the service call runs inside the producer's locks.
    entry.async_create_background_task(
        hass, deliver(), name="ha_spark notice", eager_start=False)


@callback
def async_dismiss(hass: HomeAssistant, notification_id: str) -> None:
    """Withdraw a notice, including the fallback of a delivery still in flight."""
    _pending(hass).pop(notification_id, None)
    persistent_notification.async_dismiss(hass, notification_id)
