"""Shared HA notifier with persistent-notification fallback.

All authority/hold/fallback/fault notices go through the configured
``notify.<service>`` target. When no target is set the same text is raised as
an HA persistent notification so faults are never silently dropped.
"""

from __future__ import annotations

import hashlib

from ha_spark.config import Settings
from ha_spark.energy.v2l import notification_service
from ha_spark.ha.rest import HomeAssistantRest, notify
from ha_spark.logging import get_logger

log = get_logger(__name__)


def _stable_notification_id(title: str) -> str:
    """Return a stable id for a persistent notification derived from its title."""
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"ha_spark_{digest}"


async def send_notification(
    settings: Settings,
    rest: HomeAssistantRest,
    title: str,
    message: str,
    *,
    notification_id: str | None = None,
) -> None:
    """Send ``title``/``message`` through the configured notify target, or fall back
    to an HA persistent notification when no target is set.

    Any failure is logged without the message body (which may contain secrets or
    personal information) and is never re-raised.
    """
    service = notification_service(settings)
    if service:
        domain = "notify"
        try:
            await notify(rest, service, title, message)
            return
        except Exception:  # noqa: BLE001 - a notice is never a gate
            log.warning("Notification to %s.%s failed", domain, service)
            return

    domain = "persistent_notification"
    try:
        await rest.call_service(
            domain,
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": notification_id or _stable_notification_id(title),
            },
        )
    except Exception:  # noqa: BLE001 - a notice is never a gate
        log.warning("Notification to %s.create failed", domain)
