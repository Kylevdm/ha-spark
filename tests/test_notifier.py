"""Tests for the shared HA notifier with persistent-notification fallback."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from ha_spark.config import Settings
from ha_spark.ha.notifier import _stable_notification_id, send_notification
from ha_spark.ha.rest import HomeAssistantRest

BASE = "http://ha.test/api"


@respx.mock
async def test_with_notify_target_only_that_service_is_called() -> None:
    notify_route = respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        return_value=httpx.Response(200, json=[])
    )
    persistent_route = respx.post(f"{BASE}/services/persistent_notification/create").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HomeAssistantRest(BASE, "secret-token") as rest:
        await send_notification(
            Settings(notify_service="mobile_app_phone"), rest, "Title", "Body"
        )

    assert notify_route.called
    assert not persistent_route.called
    sent = notify_route.calls.last.request
    assert json.loads(sent.content) == {"title": "Title", "message": "Body"}
    assert "secret-token" not in sent.content.decode()


@respx.mock
async def test_without_notify_target_creates_persistent_notification() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))

    async with HomeAssistantRest(BASE, "secret-token") as rest:
        await send_notification(Settings(), rest, "Title", "Body")

    paths = [call.request.url.path for call in posts.calls]
    assert paths == ["/api/services/persistent_notification/create"]
    sent = posts.calls.last.request
    payload = json.loads(sent.content)
    assert payload["title"] == "Title"
    assert payload["message"] == "Body"
    assert payload["notification_id"]
    assert "secret-token" not in sent.content.decode()


@respx.mock
async def test_persistent_notification_uses_supplied_id() -> None:
    route = respx.post(f"{BASE}/services/persistent_notification/create").mock(
        return_value=httpx.Response(200, json=[])
    )

    async with HomeAssistantRest(BASE, "token") as rest:
        await send_notification(
            Settings(), rest, "Title", "Body", notification_id="my_id"
        )

    assert json.loads(route.calls.last.request.content)["notification_id"] == "my_id"


@respx.mock
async def test_http_error_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        return_value=httpx.Response(500, text="upstream failed: secret-token")
    )

    with caplog.at_level("WARNING"):
        async with HomeAssistantRest(BASE, "secret-token") as rest:
            await send_notification(
                Settings(notify_service="mobile_app_phone"), rest, "Title", "Body"
            )

    assert "Notification to notify.mobile_app_phone failed" in caplog.text
    assert "secret-token" not in caplog.text
    assert "Body" not in caplog.text


@respx.mock
async def test_transport_error_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{BASE}/services/notify/mobile_app_phone").mock(
        side_effect=httpx.ConnectError("transport failed: secret-token")
    )

    with caplog.at_level("WARNING"):
        async with HomeAssistantRest(BASE, "secret-token") as rest:
            await send_notification(
                Settings(notify_service="mobile_app_phone"), rest, "Title", "Body"
            )

    assert "Notification to notify.mobile_app_phone failed" in caplog.text
    assert "secret-token" not in caplog.text
    assert "Body" not in caplog.text


@respx.mock
async def test_persistent_notification_failure_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.post(f"{BASE}/services/persistent_notification/create").mock(
        return_value=httpx.Response(500, text="boom")
    )

    with caplog.at_level("WARNING"):
        async with HomeAssistantRest(BASE, "token") as rest:
            await send_notification(Settings(), rest, "Title", "Body")

    assert "Notification to persistent_notification.create failed" in caplog.text
    assert "Body" not in caplog.text


def test_stable_notification_id_is_stable() -> None:
    assert _stable_notification_id("Title") == _stable_notification_id("Title")
    assert _stable_notification_id("Title") != _stable_notification_id("Other")
