"""Tests for supervised Axle export lifecycle notifications (#133)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ha_spark.energy.export_notifications import (
    ExportNotificationStore,
    make_notice,
    send_once,
)


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


class _FailingRest:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def call_service(
        self, domain: str, service: str, data: dict[str, object] | None = None
    ) -> list[object]:
        raise self._error


@pytest.mark.parametrize("failure", ["transport", "status", "malformed"])
async def test_notification_failures_never_log_the_api_key(failure: str, caplog, tmp_path) -> None:
    secret = "axle-api-key-sentinel"
    if failure == "transport":
        error = httpx.ConnectError(f"transport failed: {secret}")
    elif failure == "status":
        request = httpx.Request("POST", "http://ha.test/api/services/notify/mobile")
        response = httpx.Response(503, request=request, text=f"upstream failed: {secret}")
        error = httpx.HTTPStatusError(
            f"status failed: {secret}", request=request, response=response
        )
    else:
        error = ValueError(f"malformed notification payload: {secret}")

    start, end = _window()
    notice = make_notice(
        "accepted",
        "export|2026-09-15T18:00:00+00:00|2026-09-15T19:00:00+00:00",
        start,
        end,
    )
    store = ExportNotificationStore(str(tmp_path / "events.db"))

    with caplog.at_level("WARNING"):
        sent = await send_once(store, _FailingRest(error), "mobile_app_phone", notice)

    assert sent is False
    assert secret not in caplog.text
