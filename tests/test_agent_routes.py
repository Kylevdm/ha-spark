from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ha_spark.api.server import AGENT_CONFIG_REFUSAL, AppState, build_app
from ha_spark.config import Settings
from ha_spark.energy import sources
from ha_spark.energy.models import SLOTS_PER_DAY, LoadForecast
from tests.api_helpers import owner_client
from tests.authority_helpers import seed_settings, settings_bytes


def _state(tmp_path: Path, exposure: str = "read_act", **extra: object) -> AppState:
    options_path = tmp_path / "options.json"
    settings_kw: dict[str, object] = {
        "ha_url": "http://ha.test",
        "ha_token": "x",
        "db_path": str(tmp_path / "t.db"),
        # Master switch on: these tests exercise the exposure tiers; the
        # switch itself is pinned by test_agent_surface_off_*.
        "agent_surface": "on",
        "agent_exposure": exposure,
        **extra,
    }
    service = seed_settings(tmp_path, settings=Settings(**settings_kw))  # type: ignore[arg-type]
    return AppState(service.settings, options_path, settings_service=service)


@respx.mock
def test_read_route_available(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    with TestClient(build_app(_state(tmp_path))) as client:
        assert client.get("/agent/plan").status_code == 200


def test_act_route_absent_in_read_mode(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        assert client.post("/agent/context", json={}).status_code == 404


def test_config_route_absent_below_read_write(tmp_path: Path) -> None:
    with TestClient(build_app(_state(tmp_path, exposure="read_act"))) as client:
        assert client.post("/agent/config", json={"min_soc": 30}).status_code == 404


def test_config_route_refuses_in_read_write(tmp_path: Path) -> None:
    """#457: agents can't change configuration until owner approval exists."""
    state = _state(tmp_path, exposure="read_write", min_soc=20.0)
    with TestClient(build_app(state)) as client:
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    assert resp.status_code == 409
    assert resp.json() == AGENT_CONFIG_REFUSAL
    assert not (tmp_path / "options.json").exists()
    assert state.settings.min_soc == 20.0


@respx.mock
def test_token_required_when_configured(tmp_path: Path) -> None:
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        assert client.get("/agent/plan").status_code == 401
        ok = client.get("/agent/plan", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code == 200


def test_act_and_write_routes_absent_from_openapi_schema_in_read_mode(tmp_path: Path) -> None:
    """Read-mode must not advertise act/write tools via /openapi.json (design step #7)."""
    with TestClient(build_app(_state(tmp_path, exposure="read"))) as client:
        schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "post" not in paths.get("/agent/context", {})
    assert "post" not in paths.get("/agent/run", {})
    assert "post" not in paths.get("/agent/config", {})


def test_read_act_post_context_invalid_body_hits_real_handler_not_404_stub(
    tmp_path: Path,
) -> None:
    """At read_act, POST /agent/context must reach the real add_context handler

    (which 400s on a missing "kind" key), not the read-mode 404 stub and not a
    405 from an unmatched method. This pins that the two branches in
    _agent_router don't both register a route at the same path/method.
    """
    with TestClient(build_app(_state(tmp_path, exposure="read_act"))) as client:
        resp = client.post("/agent/context", json={})
    assert resp.status_code == 400


def test_agent_config_refusal_carries_no_config_or_secret(tmp_path: Path) -> None:
    """The read_write /agent/config refusal returns no options, so no secret."""
    state = _state(
        tmp_path,
        exposure="read_write",
        octopus_api_key="SECRET_OCTO",
        agent_api_token="SECRET_AGENT",
    )
    with TestClient(build_app(state)) as client:
        resp = client.post("/agent/config", json={"agent_exposure": "read_write"})
    assert resp.status_code == 409
    assert "SECRET_OCTO" not in resp.text
    assert "SECRET_AGENT" not in resp.text
    assert resp.json() == AGENT_CONFIG_REFUSAL


@respx.mock
def test_401_response_does_not_leak_token(tmp_path: Path) -> None:
    app = build_app(_state(tmp_path), require_token=True, token="sekret")
    with TestClient(app) as client:
        resp = client.get("/agent/plan")
    assert resp.status_code == 401
    assert "sekret" not in resp.text


@respx.mock
def test_exposure_downgrade_via_api_config_applies_without_restart(tmp_path: Path) -> None:
    """POST /api/config {"agent_exposure": "read"} shrinks the surface on the same
    client immediately: the gate is evaluated per request against live settings,
    never captured at router-build time (#93)."""
    respx.get("http://ha.test/api/states").mock(return_value=httpx.Response(200, json=[]))
    state = _state(tmp_path, exposure="read_write")
    with owner_client(state) as client:
        assert client.post("/agent/config", json={"min_soc": 30.0}).status_code == 409
        assert (
            client.post(
                "/api/config",
                json={
                    "expected_revision": state.settings_service.revision,
                    "changes": {"agent_exposure": "read"},
                },
            ).status_code
            == 200
        )
        # act + write tiers are gone...
        assert client.post("/agent/config", json={}).status_code == 404
        assert client.post("/agent/run").status_code == 404
        assert client.post("/agent/context", json={}).status_code == 404
        # ...read survives, and the schema stops advertising the denied ops.
        assert client.get("/agent/plan").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert "post" not in paths.get("/agent/run", {})
        assert "post" not in paths.get("/agent/config", {})


def test_read_write_advertises_the_full_surface_in_schema(tmp_path: Path) -> None:
    """At read_write the whole /agent/* surface is advertised (pins the schema
    filter against the registered routes -- drift here hides a route)."""
    with TestClient(build_app(_state(tmp_path, exposure="read_write"))) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert {p: set(paths[p]) for p in paths if p.startswith("/agent/")} == {
        "/agent/plan": {"get"},
        "/agent/state": {"get"},
        "/agent/forecast": {"get"},
        "/agent/predictions": {"get"},
        "/agent/health": {"get"},
        "/agent/context": {"get", "post"},
        "/agent/run": {"post"},
        "/agent/config": {"post"},
    }


def test_agent_surface_off_denies_agent_routes_and_schema(tmp_path: Path) -> None:
    """agent_surface="off" (the shipped default) is the master switch: every
    /agent/* route 404s and nothing is advertised in the schema (#93)."""
    with TestClient(build_app(_state(tmp_path, agent_surface="off"))) as client:
        assert client.get("/agent/plan").status_code == 404
        assert client.get("/agent/health").status_code == 404
        assert client.get("/agent/context").status_code == 404
        assert client.post("/agent/context", json={}).status_code == 404
        assert client.post("/agent/run").status_code == 404
        assert client.post("/agent/config", json={}).status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert not [p for p in paths if p.startswith("/agent/")]


def test_exposure_upgrade_via_api_config_applies_without_restart(tmp_path: Path) -> None:
    """The gate is not latched: raising the tier at runtime opens the new routes too."""
    state = _state(tmp_path, exposure="read")
    with owner_client(state) as client:
        assert client.post("/agent/config", json={"min_soc": 30.0}).status_code == 404
        assert (
            client.post(
                "/api/config",
                json={
                    "expected_revision": state.settings_service.revision,
                    "changes": {"agent_exposure": "read_write"},
                },
            ).status_code
            == 200
        )
        resp = client.post("/agent/config", json={"min_soc": 30.0})
    # The write tier is open now, and its one operation refuses (#457).
    assert resp.status_code == 409


@respx.mock
def test_forecast_route_serves_the_served_day_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /agent/forecast serves the served day's forecast end to end (#397):
    48 slot-of-day values plus the served day's ISO date -- a 01:00 replan
    inside Sunday night's window answers with Monday's profile, not
    Tuesday's (#200), and never the horizon-order ``load_slots``."""
    frozen = datetime(2026, 1, 12, 1, 0, tzinfo=ZoneInfo("Europe/London"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "FixedDatetime":
            return cls.fromtimestamp(frozen.timestamp(), tz or UTC)

    async def fake_load(
        _s: Settings, *, lat: float | None = None, lon: float | None = None,
        target: date | None = None,
    ) -> LoadForecast:
        return LoadForecast(
            total_kwh=12.0,
            slots=tuple(float(i) for i in range(SLOTS_PER_DAY)),
            source="slot profile (14d hourly house stats)",
            target_date=target,
        )

    monkeypatch.setattr(sources, "predict_home_load", fake_load)
    monkeypatch.setattr(sources, "datetime", FixedDatetime)
    respx.route(method="GET").mock(return_value=httpx.Response(404))

    state = _state(
        tmp_path,
        charge_window_start="23:30",
        charge_window_end="05:30",
        timezone="Europe/London",
    )
    with TestClient(build_app(state)) as client:
        body = client.get("/agent/forecast").json()

    assert body["date"] == "2026-01-12"  # Monday, the served day
    assert body["slots"] == [float(i) for i in range(SLOTS_PER_DAY)]
    assert body["load_kwh"] == 12.0
    assert body["source"] == "slot profile (14d hourly house stats)"


@pytest.mark.parametrize(
    "updates", [{"min_soc": "not-a-number"}, {"db_path": "TOPSECRET/moved-db.db"}]
)
def test_agent_config_refusal_leaves_options_and_settings_untouched(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    """#457: every /agent/config change is refused before anything is read or
    saved, so the options file stays byte-identical and the running settings
    untouched (the #412/#441 save rules now apply only to owner saves), and the
    response never echoes the submitted values or a stored secret.
    """
    state = _state(
        tmp_path, exposure="read_write", min_soc=20.0, octopus_api_key="SECRET_OCTO"
    )
    seeded = settings_bytes(tmp_path)
    with TestClient(build_app(state)) as client:
        resp = client.post("/agent/config", json=updates)
    assert resp.status_code == 409
    assert "not-a-number" not in resp.text
    assert "TOPSECRET" not in resp.text
    assert "SECRET_OCTO" not in resp.text
    assert settings_bytes(tmp_path) == seeded
    assert state.settings.min_soc == 20.0
    assert state.settings.db_path == str(tmp_path / "t.db")
