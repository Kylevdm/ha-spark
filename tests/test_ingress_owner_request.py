"""All ingress POSTs share the same ordered owner-request checks."""

from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.daemon_channel import Reply
from tests.api_helpers import owner_client
from tests.test_owner_mode import settings_at

ROUTES = [
    ("/api/mode", "mode", {"target": "observe", "expected_epoch": 1}),
    ("/api/replan", "replan", {}),
    ("/api/config", "config", {"expected_revision": 1, "changes": {}}),
    ("/api/config/import", "config", {"expected": 1, "document": {}}),
]


@pytest.fixture
def guarded_state(tmp_path, monkeypatch):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    state.executor = AsyncMock()
    state.executor.submit_mode.return_value = Reply(authority={})
    state.executor.submit.return_value = Reply()
    save = Mock(return_value={})
    import_document = Mock(return_value={})
    monkeypatch.setattr(state.settings_service, "save", save)
    monkeypatch.setattr(state.settings_service, "import_document", import_document)
    return state, save, import_document


@pytest.mark.parametrize("path,kind,body", ROUTES)
@pytest.mark.parametrize("header,value,status", [
    ("X-Remote-User-Id", None, 403),
    ("X-Remote-User-Id", "a" * 65, 403),
    ("X-Remote-User-Id", "not an owner", 403),
    ("X-Remote-User-Id", "owner\n", 403),
    ("X-HA-Spark-Request", None, 403),
    ("X-HA-Spark-Request", "wrong", 403),
    ("Content-Type", None, 415),
    ("Content-Type", "text/plain", 415),
])
def test_owner_request_refusals(guarded_state, path, kind, body, header, value, status):
    state, save, import_document = guarded_state
    with owner_client(state) as client:
        # Clear the helper's defaults so missing headers really are absent.
        client.headers.clear()
        headers = {"X-Remote-User-Id": "owner", "X-HA-Spark-Request": kind,
                   "Content-Type": "application/json"}
        if value is None:
            del headers[header]
        else:
            headers[header] = value
        # Raw content avoids httpx adding Content-Type for a JSON body.
        response = client.post(path, content="{}", headers=headers)
    assert response.status_code == status
    assert response.json() == {"error": "forbidden" if status == 403
                               else "application/json required"}
    assert state.executor.mock_calls == []
    save.assert_not_called()
    import_document.assert_not_called()


@pytest.mark.parametrize("path,kind,body,other_kind", [
    (*route, other_kind)
    for route in ROUTES
    for other_kind in ("mode", "replan", "config")
    if route[1] != other_kind
])
def test_owner_request_rejects_other_kinds(guarded_state, path, kind, body, other_kind):
    state, save, import_document = guarded_state
    with owner_client(state) as client:
        response = client.post(path, json=body, headers={"X-HA-Spark-Request": other_kind})
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert state.executor.mock_calls == []
    save.assert_not_called()
    import_document.assert_not_called()


@pytest.mark.parametrize("path,kind,body", ROUTES)
@pytest.mark.parametrize("user,request_kind", [(None, None), ("owner", "wrong")])
def test_owner_request_checks_identity_and_kind_before_media_type(
    guarded_state, path, kind, body, user, request_kind,
):
    state, save, import_document = guarded_state
    with owner_client(state) as client:
        client.headers.clear()
        headers = {}
        if user is not None:
            headers["X-Remote-User-Id"] = user
        if request_kind is not None:
            headers["X-HA-Spark-Request"] = request_kind
        response = client.post(path, content="not JSON", headers=headers)
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert state.executor.mock_calls == []
    save.assert_not_called()
    import_document.assert_not_called()


@pytest.mark.parametrize("path,kind,body", ROUTES)
def test_owner_request_preserves_validated_user(guarded_state, path, kind, body):
    state, save, import_document = guarded_state
    user = "A_0-" + "z" * 60
    with owner_client(state) as client:
        response = client.post(path, json=body, headers={
            "X-Remote-User-Id": user, "X-HA-Spark-Request": kind,
            "Content-Type": "Application/JSON; charset=utf-8",
        })
    assert response.status_code == 200
    if path == "/api/mode":
        assert state.executor.submit_mode.call_args.kwargs == {"ingress_user": user}
    elif path == "/api/replan":
        state.executor.submit.assert_awaited_once_with("replan", ingress_user=user)
    elif path == "/api/config":
        save.assert_called_once_with({}, 1, f"ingress:{user}")
    else:
        import_document.assert_called_once_with({}, 1, f"ingress:{user}")


@pytest.mark.parametrize("path,kind,body", ROUTES)
@pytest.mark.parametrize("listener", ["agent", "standalone"])
def test_owner_posts_are_ingress_only(guarded_state, path, kind, body, listener):
    state, save, import_document = guarded_state
    kwargs = {"require_token": True, "token": "test-token", "ingress_peer": "testclient"}
    app = build_app(state, **kwargs) if listener == "agent" else build_app(state)
    with TestClient(app) as client:
        response = client.post(path, json=body, headers={
            "Authorization": "Bearer test-token", "X-Remote-User-Id": "owner",
            "X-HA-Spark-Request": kind,
        })
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert state.executor.mock_calls == []
    save.assert_not_called()
    import_document.assert_not_called()
