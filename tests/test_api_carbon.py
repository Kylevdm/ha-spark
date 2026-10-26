"""The carbon cache is exposed only to ingress, never during repair."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.energy.carbon import CarbonRefresher
from tests.test_carbon import NOW, envelope, row
from tests.test_owner_mode import settings_at


@pytest.mark.parametrize("token,peer", [(False, None), (True, None), (True, "testclient")])
def test_carbon_absent_outside_ingress(tmp_path, token, peer):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    with TestClient(build_app(state, require_token=token, token="TOKEN", ingress_peer=peer)) as c:
        assert c.get("/api/carbon", headers={"Authorization": "Bearer TOKEN"}).status_code == 404


def test_carbon_peer_and_repair(tmp_path):
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    with TestClient(build_app(state, ingress_peer="other")) as c:
        assert c.get("/api/carbon", headers={
            "X-Forwarded-For": "testclient", "X-Remote-User-Id": "owner",
        }).status_code == 403
    state.settings_service.repair = ["invalid configuration"]
    with TestClient(build_app(state, ingress_peer="testclient")) as c:
        assert c.get("/api/carbon").status_code == 503


async def test_carbon_http_statuses(tmp_path, monkeypatch):
    import httpx
    import respx

    from ha_spark.api import server

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr(server, "datetime", Clock)
    state = AppState(settings_at(tmp_path), tmp_path / "options.json")
    with TestClient(build_app(state, ingress_peer="testclient")) as c:
        assert c.get("/api/carbon").json()["status"] == "off"
        state.settings = state.settings.model_copy(update={"carbon_region": 12})
        assert c.get("/api/carbon").json()["status"] == "unavailable"
        refresher = CarbonRefresher(state.settings.db_path, lambda: 12)
        with respx.mock:
            respx.get(url__startswith="https://api.carbonintensity.org.uk/").mock(
                return_value=httpx.Response(200, json=envelope([row()])),
            )
            refresher.tick(NOW - timedelta(hours=3), "Europe/London", None)
            await refresher.wait()
            assert c.get("/api/carbon").json()["status"] == "stale"
            refresher.tick(NOW, "Europe/London", None)
            await refresher.wait()
        payload = c.get("/api/carbon").json()
        assert payload["status"] == "ok"
        assert payload["region"] == 12
        assert payload["ends_at"] == payload["rows"][-1]["end"]
        assert "CC BY 4.0" in payload["attribution"]
        await refresher.close()
