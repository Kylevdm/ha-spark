"""Consume the integration's export transition log as #133 lifecycle notices.

The integration logs verified transitions; this daemon-loop pass sends them in
order, acknowledges ids only after HA accepted (or the durable store already
sent) them, and never lets a notification failure affect control.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ha_spark.api.contract import IntegrationLink
from ha_spark.api.snapshot_v4 import Event, ExportTransition
from ha_spark.energy.export_notifications import (
    ExportNotificationStore,
    make_notice,
    send_once,
)
from ha_spark.energy.export_notifications import (
    ExportTransition as NoticeTransition,
)
from ha_spark.ha.rest import HomeAssistantRest

if TYPE_CHECKING:
    from ha_spark.config import Settings

_NOTICE_KIND: dict[str, NoticeTransition] = {
    "accepted": "accepted", "started": "started", "cleanup_verified": "cleanup",
    "clock_hold": "clock", "aborted": "aborted", "tripped": "aborted",
}
_TRIPPED_REASON = "site export over limit"
# The integration's fixed refusal codes (commands.WriteReason) plus the failure
# outcomes; any other wire value is shown as the generic text, never echoed.
_REASONS = frozenset({
    "not_available", "invalid_intent", "limits", "mode", "stale_epoch", "authority_unknown",
    "gateway_not_owned", "read_only", "untrusted_soc", "grid_charge_not_permitted",
    "not_supervised", "export_bound_unverified", "export_bounds_exceed_site_limit",
    "clock_not_trusted", "hold_overlaps_export", "hold_data_untrusted",
    "power_switch_not_on", "tripped", "export_not_available", "unverified", "failed",
})
_SAFE_STATE = {
    True: "no export window remains programmed",
    False: "the Solis export state was not verified; switch to Observe and inspect it",
}


def _safe_state(cleared: bool | None) -> str | None:
    if cleared is None:
        return None
    return _SAFE_STATE[cleared]


def event_id(event: Event) -> str:
    """The add-on's original event identity, independent of any newer record."""
    return (f"{event.direction}|{event.start.astimezone(UTC).isoformat()}|"
            f"{event.end.astimezone(UTC).isoformat()}")


async def _send(
    store: ExportNotificationStore, rest: HomeAssistantRest, service: str,
    row: ExportTransition, identity: str, start: datetime, end: datetime,
    planned_for: Callable[[str], tuple[float | None, float | None]],
) -> bool:
    """True when this id can be acknowledged, False when a later pass must retry."""
    transition = _NOTICE_KIND.get(row.kind)
    if transition is None:
        return True  # armed and unknown kinds need no notice
    if not service:
        return True
    if await store.sent(identity, transition):
        return True
    planned_kw, limit_kw = planned_for(identity)
    reason = (_TRIPPED_REASON if row.kind == "tripped"
              else row.reason if row.reason in _REASONS else None)
    safe_state = _safe_state(row.cleared) if row.kind == "aborted" else None
    notice = make_notice(
        transition, identity, start, end, planned_export_kw=planned_kw,
        dno_export_limit_kw=limit_kw, reason=reason, safe_state=safe_state,
    )
    sent = await send_once(store, rest, service, notice)
    return sent or await store.sent(identity, transition)


async def notify_export_transitions(
    settings: Settings, rest: HomeAssistantRest, link: IntegrationLink, *,
    acknowledged: set[str],
    planned_for: Callable[[str], tuple[float | None, float | None]],
) -> None:
    """Send unacknowledged transitions in order; never raise on a bad payload."""
    if link.v4 is None or link.v4.owner != "integration":
        return
    store = ExportNotificationStore(settings.db_path)
    # Oldest first across inverters, so one event's rows meet the dedupe store together.
    rows = sorted((row for entry in link.v4.export for row in entry.transitions),
                  key=lambda row: row.at)
    for row in rows:
        if row.id in acknowledged:
            continue
        identity = event_id(row.event)
        if not await _send(store, rest, settings.notify_service, row, identity,
                           row.event.start, row.event.end, planned_for):
            return  # HA refused or timed out: retry next pass, never stall the loop
        acknowledged.add(row.id)
