"""Tests for supervised Axle export lifecycle notifications (#133)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from ha_spark.energy.export_notifications import (
    ExportNotificationStore,
    make_notice,
    send_once,
)
from ha_spark.ha.rest import HomeAssistantRest

HA = "http://ha.test/api"


class FakeRest:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[object]:
        self.calls.append((domain, service, data or {}))
        return []


def _window() -> tuple[datetime, datetime]:
    start = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
    return start, start + timedelta(hours=1)


async def test_send_once_deduplicates_by_event_and_transition(tmp_path) -> None:
    rest = FakeRest()
    store = ExportNotificationStore(str(tmp_path / "events.db"))
    start, end = _window()
    accepted = make_notice(
        "accepted",
        "export|2026-09-15T18:00:00+00:00|2026-09-15T19:00:00+00:00",
        start,
        end,
        planned_export_kw=3.2,
        dno_export_limit_kw=7.36,
    )

    assert await send_once(store, rest, "mobile_app_phone", accepted) is True
    assert await send_once(store, rest, "mobile_app_phone", accepted) is False

    started = make_notice("started", accepted.event_id, start, end)
    assert await send_once(store, rest, "mobile_app_phone", started) is True

    assert [(domain, service) for domain, service, _ in rest.calls] == [
        ("notify", "mobile_app_phone"),
        ("notify", "mobile_app_phone"),
    ]
    assert "3.2 kW" in rest.calls[0][2]["message"]
    assert "7.36 kW" in rest.calls[0][2]["message"]

    # A fresh process sees the same durable dedupe state.
    assert (
        await send_once(
            ExportNotificationStore(str(tmp_path / "events.db")),
            rest,
            "mobile_app_phone",
            accepted,
        )
        is False
    )


async def test_notice_contract_covers_abort_and_cleanup_safe_state() -> None:
    start, end = _window()
    aborted = make_notice(
        "aborted",
        "event-1",
        start,
        end,
        reason="power_switch is 'Off'",
        safe_state="no export window was programmed",
    )
    cleaned = make_notice(
        "cleanup",
        "event-1",
        start,
        end,
        safe_state="the timed export window was cleared and read back",
    )

    assert aborted.title == "Axle export aborted"
    assert "power_switch is 'Off'" in aborted.message
    assert "no export window was programmed" in aborted.message
    assert cleaned.title == "Axle export cleanup verified"
    assert "cleared and read back" in cleaned.message


@pytest.mark.parametrize("failure", ["transport", "status", "malformed"])
@respx.mock
async def test_notification_failures_never_log_the_api_key(failure: str, caplog, tmp_path) -> None:
    auth_token = "ha-auth-token-sentinel"
    axle_api_key = "axle-api-key-sentinel"
    route = respx.post(f"{HA}/services/notify/mobile_app_phone")
    if failure == "transport":
        route.mock(
            side_effect=httpx.ConnectError(
                f"transport failed: {auth_token} {axle_api_key}"
            )
        )
    elif failure == "status":
        route.mock(
            return_value=httpx.Response(
                503, text=f"upstream failed: {auth_token} {axle_api_key}"
            )
        )
    else:
        route.mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "sensor.notification",
                        "state": "on",
                        "attributes": axle_api_key,
                    }
                ],
            )
        )

    start, end = _window()
    notice = make_notice(
        "accepted",
        "export|2026-09-15T18:00:00+00:00|2026-09-15T19:00:00+00:00",
        start,
        end,
    )
    store = ExportNotificationStore(str(tmp_path / "events.db"))

    with caplog.at_level("INFO"):
        async with HomeAssistantRest(HA, auth_token) as rest:
            sent = await send_once(store, rest, "mobile_app_phone", notice)

    assert sent is False
    assert auth_token not in caplog.text
    assert axle_api_key not in caplog.text
