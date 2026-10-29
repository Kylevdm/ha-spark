"""HTTP security boundary for the companion heartbeat (#476)."""

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ha_spark.api import server
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import load_authority
from ha_spark.config import Settings
from ha_spark.energy.scheduler import _ingress_server
from ha_spark.integration_discovery import IntegrationDiscovery

FIXTURES = Path(__file__).parent / "fixtures" / "contract"
CANARY = "CANARY-HEARTBEAT-SECRET"
SNAPSHOT_BODY = (FIXTURES / "site_snapshot.json").read_bytes()


@pytest.fixture
def discovery() -> IntegrationDiscovery:
    data = json.loads((FIXTURES / "discovery.json").read_text())
    return IntegrationDiscovery(supervisor_token="unused", host=data["host"],
                                auth_token=data["auth_token"], session_id=data["session_id"])


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    settings = Settings(supervisor_token="test", db_path=str(tmp_path / "ha_spark.db"))
    return AppState(settings, tmp_path / "options.json")


@pytest.fixture
def standalone(tmp_path: Path) -> AppState:
    settings = Settings(ha_url="http://ha.test", ha_token="test",
                        db_path=str(tmp_path / "ha_spark.db"))
    return AppState(settings, tmp_path / "options.json")


async def request(state: AppState, discovery: IntegrationDiscovery, *, peer: str = "other",
                  method: str = "POST", path: str = "/integration/heartbeat",
                  headers: dict[str, str] | httpx.Headers | None = None,
                  content: bytes | AsyncIterator[bytes] = SNAPSHOT_BODY) -> httpx.Response:
    app = build_app(state, ingress_peer="proxy", integration=discovery)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1)),
                                 base_url="http://addon") as client:
        return await client.request(method, path, headers=headers, content=content)


@pytest.mark.parametrize("peer", ["proxy", "other"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "bearer x", "Token x", "x"])
async def test_invalid_token(state, discovery, peer, authorization, caplog):
    caplog.set_level(logging.DEBUG)
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await request(state, discovery, peer=peer, headers=headers)
    assert response.status_code == 401
    assert discovery.auth_token not in response.text + caplog.text


async def test_non_ascii_token_refused(state, discovery, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery,
                             headers=httpx.Headers({"Authorization": b"Bearer \xff"}))
    assert response.status_code == 401
    assert discovery.auth_token not in response.text + caplog.text


@pytest.mark.parametrize("header", ["X-Ingress-Path", "X-Hass-Source", "X-Remote-User-Id",
                                     "X-Remote-User-Name", "x-ReMoTe-UsEr-Name"])
async def test_ingress_headers_refused(state, discovery, header, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery, headers={
        "Authorization": f"Bearer {discovery.auth_token}", header: CANARY})
    assert response.status_code == 403
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


@pytest.mark.parametrize("peer", ["proxy", "other"])
async def test_authenticated_peer(state, discovery, peer):
    response = await request(state, discovery, peer=peer,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200


@pytest.mark.parametrize("version", [1, 2, 3])
def test_summary_uses_request_version(state, discovery, monkeypatch, version):
    monkeypatch.setattr(server, "SUPPORTED_API_VERSIONS", frozenset({1, 2, 3}))
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    snapshot = {**json.loads(SNAPSHOT_BODY), "api_version": version}
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        response = client.post(
            "/integration/heartbeat", json=snapshot,
            headers={"Authorization": f"Bearer {discovery.auth_token}"},
        )
    assert response.status_code == 200
    assert response.json()["api_version"] == version


@pytest.mark.parametrize("method,path", [("GET", "/api/mode"), ("POST", "/api/mode"),
    ("GET", "/api/config"), ("GET", "/api/health"), ("GET", "/"),
    ("GET", "/agent/health"), ("POST", "/mcp")])
async def test_token_cannot_open_ingress(state, discovery, method, path):
    response = await request(state, discovery, method=method, path=path,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 403


def test_agent_and_standalone_separation(state, standalone, discovery):
    with pytest.raises(ValueError):
        build_app(state, ingress_peer="proxy", integration=discovery, require_token=True)
    with pytest.raises(ValueError):
        build_app(state, integration=discovery)
    with pytest.raises(ValueError):
        build_app(standalone, ingress_peer="proxy", integration=discovery)
    with TestClient(build_app(state, require_token=True, token="agent")) as client:
        for path in ("/agent/health", "/mcp"):
            assert client.get(path, headers={
                "Authorization": f"Bearer {discovery.auth_token}"}).status_code == 401
        assert client.post("/integration/heartbeat", headers={
            "Authorization": "Bearer agent"}, json={}).status_code == 404
    listener = _ingress_server(standalone, discovery)
    assert listener.config.host == "127.0.0.1"
    with TestClient(listener.config.app) as client:
        assert client.post("/integration/heartbeat", json={}).status_code == 404


@pytest.mark.parametrize("body", [b"not-json-CANARY-HEARTBEAT-SECRET", b"[]",
    b'"CANARY-HEARTBEAT-SECRET"', b"[" * 3000 + b"]" * 3000,
    CANARY.encode() + b"x" * 32769, b'{"x": NaN}', b'{"x": -Infinity}'],
    ids=["invalid-json", "array", "string", "deep-nesting", "content-length-cap", "nan",
         "infinity"])
async def test_invalid_body(state, discovery, body, caplog):
    caplog.set_level(logging.DEBUG)
    response = await request(state, discovery, content=body,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


async def test_chunked_body_cap(state, discovery, caplog):
    caplog.set_level(logging.DEBUG)

    async def chunks() -> AsyncIterator[bytes]:
        yield CANARY.encode() + b"x" * 32768
        raise AssertionError("must stop reading at the cap")

    response = await request(state, discovery, content=chunks(),
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 400
    assert CANARY not in response.text + caplog.text


async def test_integration_namespace(state, discovery):
    headers = {"Authorization": f"Bearer {discovery.auth_token}"}
    assert (await request(state, discovery, method="GET", headers=headers)).status_code == 405
    assert (await request(state, discovery, path="/integration/other",
                          headers=headers)).status_code == 404
    assert (await request(state, discovery, path="/integration/other")).status_code == 401
    trailing = await request(state, discovery, path=f"/integration/heartbeat/?q={CANARY}",
                             headers=headers)
    assert trailing.status_code == 404
    assert CANARY not in str(trailing.headers) + trailing.text
    with TestClient(build_app(state, ingress_peer="proxy", integration=discovery)) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/integration/heartbeat"):
                pytest.fail("websocket accepted")
        assert exc.value.code == 1008


@pytest.mark.parametrize("authorization,ingress_header,expected", [
    (False, False, 401), (True, False, 503), (True, True, 403),
])
async def test_repair_auth_order(state, discovery, authorization, ingress_header, expected, caplog):
    caplog.set_level(logging.DEBUG)
    state.settings_service.repair = ["invalid configuration"]
    headers = {"Authorization": f"Bearer {discovery.auth_token}"} if authorization else {}
    if ingress_header:
        headers["X-Ingress-Path"] = CANARY
    response = await request(state, discovery, headers=headers, content=CANARY.encode())
    assert response.status_code == expected
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text


async def test_agent_token_cannot_open_integration(tmp_path, discovery, caplog):
    caplog.set_level(logging.DEBUG)
    settings = Settings(supervisor_token="test", agent_surface="on",
                        agent_api_token=CANARY, db_path=str(tmp_path / "ha_spark.db"))
    state = AppState(settings, tmp_path / "options.json")
    response = await request(state, discovery, headers={"Authorization": f"Bearer {CANARY}"})
    assert response.status_code == 401
    assert CANARY not in response.text + caplog.text


@pytest.mark.parametrize("version", [1, 2, 3])
async def test_golden_envelope_and_no_mutation(state, discovery, monkeypatch, version):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    state.loop_tick = datetime(2026, 10, 24, 11, 59, 30, tzinfo=UTC)
    from ha_spark.energy.v2l import V2LSession, save_session
    from tests.test_api import _plan

    state.plan = _plan(planned_cost=1.234, baseline_cost=3.456, strategy=CANARY)
    state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    state.soc_observation_ok = True
    entity_settings = {key: f"sensor.{CANARY}" for key in type(state.settings).model_fields
                       if key.endswith("_entity")}
    state.settings = state.settings.model_copy(update=entity_settings | {
        "v2l_power_entity": f"sensor.{CANARY}", "v2l_rectifier_efficiency": 1.0,
        "charge_efficiency": 1.0, "v2l_round_trip_efficiency": 1.0,
        "v2l_peak_rate_gbp": 0.3, "v2l_offpeak_rate_gbp": 0.1,
    })
    save_session(state.settings, V2LSession(day="2026-10-24", kwh_delivered=2.345,
        peak_power_w=1234.5, last_sample_ts=state.loop_tick.isoformat()))
    before = (state.current_options(), state.settings_service.revision,
              state.settings.model_dump(), state.plan, state.plan_at)
    authority_before = load_authority(state.settings)
    # Planning-input-like fields ride along; they must be dropped, never applied.
    injected = json.loads(SNAPSHOT_BODY) | {"api_version": version,
                                            "mode": "optimise", "operating_mode": "optimise",
                                            "fallback": {"active": True}, "holds": [CANARY]}
    response = await request(state, discovery, content=json.dumps(injected).encode(),
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    name = f"plan_summary_v{version}.json" if version < 3 else "plan_summary.json"
    golden = json.loads((FIXTURES / name).read_text())
    assert response.json() == golden
    assert CANARY not in response.text
    contract = json.loads((FIXTURES / "contract.json").read_text())
    assert golden["api_version"] == version
    assert contract["api_version"] == 3
    assert (state.current_options(), state.settings_service.revision,
            state.settings.model_dump(), state.plan, state.plan_at) == before
    assert load_authority(state.settings) == authority_before


@pytest.mark.parametrize("extra", [{}, {"nested": {"secret": CANARY}, "mode": "optimise"}])
def test_snapshot_accepted_and_unknown_fields_dropped(state, discovery, extra):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", json=snapshot | extra,
                               headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    stored = state.integration_link.snapshot
    assert stored is not None
    known = stored.model_dump()
    identity = {k: v for k, v in snapshot.items() if k not in (
        "topology", "major_loads", "supply_guard",
    )}
    assert known == identity | {"sent_at": datetime.fromisoformat(snapshot["sent_at"])}
    assert state.integration_link.received_at is not None
    assert state.integration_link.received_at.utcoffset().total_seconds() == 0
    assert not state.integration_link.skew
    assert snapshot["api_version"] == json.loads((FIXTURES / "contract.json").read_text())[
        "api_version"]


@pytest.mark.parametrize("tick", [None, datetime(2026, 10, 24, 11, 50, tzinfo=UTC)])
def test_http_reports_loop_tick_without_refreshing(state, discovery, monkeypatch, tick):
    monkeypatch.setattr(server, "_heartbeat_now", lambda: datetime(2026, 10, 24, 12, tzinfo=UTC))
    assert state.loop_tick is None
    state.loop_tick = tick
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        for _ in range(2):
            response = client.post("/integration/heartbeat",
                                   json=json.loads((FIXTURES / "site_snapshot.json").read_text()),
                                   headers={"Authorization": f"Bearer {discovery.auth_token}"})
            assert response.json()["loop_tick"] == (tick.isoformat() if tick else None)
    assert state.loop_tick == tick


@pytest.mark.parametrize("version", [4, 99])
def test_version_skew_keeps_snapshot_and_returns_refusal(state, discovery, version):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        accepted = client.post("/integration/heartbeat", json=snapshot, headers=headers)
        assert accepted.status_code == 200
        previous = state.integration_link
        # No v1 body: a newer version is classified using only the frozen envelope.
        response = client.post("/integration/heartbeat", headers=headers, json={
            "api_version": version, "release_version": "1.0.0", "site_id": [],
            "sent_at": False, "v2": {"new": True}})
    assert response.status_code == 409
    refusal = json.loads((FIXTURES / "version_refusal_v3.json").read_text())
    assert response.json() == refusal
    assert refusal["api_version"] == json.loads((FIXTURES / "contract.json").read_text())[
        "api_version"]
    assert state.integration_link.skew
    assert state.integration_link.snapshot == previous.snapshot
    assert state.integration_link.received_at == previous.received_at


@pytest.mark.parametrize("field,value", [
    ("api_version", None), ("api_version", "1"), ("api_version", True),
    ("api_version", 1.0), ("api_version", 0), ("api_version", -1),
    ("release_version", None), ("release_version", ""), ("release_version", "x" * 65),
    ("release_version", 1), ("release_version", "0.25.0\x1b[2J"),
    ("release_version", "0.25.0 build"), ("site_id", CANARY), ("site_id", None),
    ("site_id", 123), ("sent_at", "2026-10-24T12:00:00"), ("sent_at", 123),
    ("sent_at", None), ("sent_at", "0001-01-01T00:00:00+14:00"),
    ("sent_at", "9999-12-31T23:59:59-14:00"), ("sent_at", "2026-10-24T12:00:00+00:60"),
    ("sent_at", "2026-10-24T12:00:00+01:99"), ("sent_at", "2026-10-24T12:00:00+00:00:60"),
])
def test_invalid_snapshot_does_not_mutate_or_echo(state, discovery, caplog, field, value):
    caplog.set_level(logging.DEBUG)
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        accepted = client.post("/integration/heartbeat", json=snapshot, headers=headers)
        assert accepted.status_code == 200
        previous = state.integration_link
        snapshot["canary"] = CANARY
        if value is None:
            del snapshot[field]
        else:
            snapshot[field] = value
        response = client.post("/integration/heartbeat", json=snapshot, headers=headers)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}
    assert CANARY not in response.text + caplog.text
    assert discovery.auth_token not in response.text + caplog.text
    assert state.integration_link is previous


def test_snapshot_normalises_uuid_and_time(state, discovery):
    snapshot = json.loads((FIXTURES / "site_snapshot.json").read_text())
    snapshot["site_id"] = snapshot["site_id"].replace("-", "").upper()
    snapshot["sent_at"] = "2026-10-24T12:59:58+01:00"
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        assert client.post("/integration/heartbeat", json=snapshot, headers={
            "Authorization": f"Bearer {discovery.auth_token}"}).status_code == 200
    stored = state.integration_link.snapshot
    assert stored is not None
    assert stored.site_id == "6f1c2b3a-9d84-4e57-a1c0-2f3e4d5c6b7a"
    assert stored.sent_at == datetime(2026, 10, 24, 11, 59, 58, tzinfo=UTC)
    assert stored.sent_at.tzinfo == UTC


@pytest.mark.parametrize("configured,sampled", [(False, False), (True, False), (True, True)])
def test_number_blocks_nulls_and_current_soc(state, discovery, configured, sampled):
    from ha_spark.energy.v2l import V2LSession, save_session
    from tests.test_api import _plan

    state.settings = state.settings.model_copy(update={
        "v2l_power_entity": "sensor.private" if configured else "",
    })
    if sampled:
        save_session(state.settings, V2LSession(day="2026-10-24", last_sample_ts="sample"))
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        headers = {"Authorization": f"Bearer {discovery.auth_token}"}
        def poll():
            return client.post("/integration/heartbeat", content=SNAPSHOT_BODY,
                               headers=headers).json()
        body = poll()
        assert body["plan"] is None
        assert (body["v2l"] is None) == (not configured or not sampled)
        state.plan = _plan()
        state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
        state.soc_observation_ok = True
        assert poll()["plan"]["soc_trusted"] is True
        state.soc_observation_ok = False
        body = poll()
        assert body["plan"]["soc_trusted"] is False
        assert body["plan"]["charge_needed_kwh"] == 9.6
        assert body["plan"]["planned_cost_gbp"] is None
        assert body["plan"]["baseline_cost_gbp"] is None
        assert "sensor.private" not in json.dumps(body)


@pytest.mark.parametrize("field", [
    "required_kwh", "target_soc", "load_kwh", "effective_solar_kwh", "deficit_kwh",
    "planned_cost", "baseline_cost",
])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
async def test_non_finite_plan_number_is_null_and_heartbeat_counts(state, discovery, field, bad):
    from dataclasses import replace

    from tests.test_api import _plan

    state.plan = replace(_plan(planned_cost=1.0, baseline_cost=2.0), **{field: bad})
    state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    plan = response.json()["plan"]
    wire_key = {
        "required_kwh": "charge_needed_kwh", "target_soc": "target_soc_pct",
        "load_kwh": "forecast_load_kwh", "effective_solar_kwh": "solar_forecast_kwh",
        "deficit_kwh": "deficit_kwh", "planned_cost": "planned_cost_gbp",
        "baseline_cost": "baseline_cost_gbp",
    }[field]
    assert plan[wire_key] is None
    # The new next-change block is legitimately null when no charge can be scheduled.
    assert all(value is not None for key, value in plan.items()
               if key not in {wire_key, "next_change"})


@pytest.mark.parametrize("session", [
    {"kwh_delivered": "bad"}, {"kwh_delivered": True}, {"kwh_delivered": float("inf")},
    {"peak_power_w": None}, {"kwh_delivered": 1.7e308}, {"peak_power_w": float("nan")},
])
async def test_hostile_session_file_nulls_v2l_and_heartbeat_counts(state, discovery, session):
    state.settings = state.settings.model_copy(update={"v2l_power_entity": "sensor.v2l"})
    path = Path(state.settings.db_path).parent / "ha_spark_v2l_session.json"
    path.write_text(json.dumps({"day": "2026-10-24", "last_sample_ts": "x", **session}))
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.json()["v2l"] is None


async def test_session_file_unread_without_v2l(state, discovery, caplog):
    caplog.set_level(logging.DEBUG)
    path = Path(state.settings.db_path).parent / "ha_spark_v2l_session.json"
    path.write_text(json.dumps({"day": "x", CANARY: 1}))
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.json()["v2l"] is None
    assert CANARY not in caplog.text
    assert "V2L session" not in caplog.text


def test_status_uses_store_hold_in_observe_without_mutation(
    state, discovery, monkeypatch, tmp_path,
):
    from ha_spark.authority import AuthorityStore
    from tests.test_api import _plan
    from tests.test_plan_status import at, record

    monkeypatch.setattr(server, "_heartbeat_now", lambda: at(12))
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    store = AuthorityStore(state.settings)
    store.initialise()
    store.save_holds((record("ev"),))
    state.plan = _plan()
    state.plan_at = at(11)
    before = store.store_path.read_bytes()
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        response = client.post("/integration/heartbeat", content=SNAPSHOT_BODY,
                               headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["status"]["action"] == "hold"
    assert plan["status"]["reason"] == "Battery held while the EV charges."
    assert plan["next_change"] is None
    assert CANARY not in response.text
    assert store.store_path.read_bytes() == before
    assert store.state.committed_mode == "observe"


@pytest.mark.parametrize("soc_status", ["implausible", "unavailable"])
def test_status_text_is_bounded_and_export_soc_is_paired(state, discovery, monkeypatch, soc_status):
    from dataclasses import replace

    from ha_spark.energy.soc_integrity import SocStatus
    from tests.test_api import _plan
    from tests.test_plan_status import at

    monkeypatch.setattr(server, "_heartbeat_now", lambda: at(12))
    plan = _plan(export_soc_needed_pct=78.6, export_soc_needed_at=at(14))
    soc = replace(plan.soc, status=SocStatus(soc_status),
                  raw_state="safe\u0001" + "x" * 300 + CANARY,
                  energy_reason="safe\u0001" + "x" * 300 + CANARY)
    state.plan = replace(plan, soc=soc)
    state.plan_at = at(11)
    with TestClient(build_app(state, ingress_peer="testclient", integration=discovery)) as client:
        def poll():
            response = client.post("/integration/heartbeat", content=SNAPSHOT_BODY,
                                   headers={"Authorization": f"Bearer {discovery.auth_token}"})
            assert response.status_code == 200
            return response.json()["plan"]["status"]
        status = poll()
        assert CANARY not in status["soc_reason"]
        if soc_status == "unavailable":
            # A reason that would quote the raw HA state is replaced by fixed text.
            assert status["soc_reason"] == "SoC sensor unavailable"
        else:
            assert len(status["soc_reason"]) == 200
            assert status["soc_reason"].isprintable()
            assert status["soc_reason"] == "".join(
                c for c in soc.reason if c.isprintable()
            )[:200]
        if soc_status == "implausible":
            assert status["soc_reason"] == "safe" + "x" * 196
        assert status["export_soc_needed_pct"] == 79
        assert status["export_soc_needed_at"] == at(14).isoformat()
        assert CANARY not in json.dumps(status)
        for pct, end in ((None, at(14)), (78.6, None)):
            state.plan = replace(state.plan, export_soc_needed_pct=pct, export_soc_needed_at=end)
            status = poll()
            assert status["export_soc_needed_pct"] is None
            assert status["export_soc_needed_at"] is None


async def test_unloadable_timezone_and_unprintable_soc_reason_degrade(state, discovery):
    from dataclasses import replace

    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
    from tests.test_api import _plan

    state.settings = state.settings.model_copy(update={"timezone": "Not/AZone"})
    plan = _plan()
    soc = SocMeasurement(SocStatus.IMPLAUSIBLE, datetime(2026, 10, 24, 11, 30, tzinfo=UTC),
                         energy_reason="\x1b\x07\x00")
    state.plan = replace(plan, soc=soc, charge_intent=replace(plan.charge_intent, soc=soc))
    state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.json()["plan"]["status"]["soc_reason"] == "implausible"


async def test_odd_plan_content_degrades_and_still_parses(state, discovery):
    """Unrenderable SoC reason, unrepresentable export times and a naive export-SoC
    time each degrade, and the heartbeat stays 200 (the harness parses a null action)."""
    from dataclasses import replace
    from datetime import timedelta, timezone

    from ha_spark.energy.models import ExportIntent
    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
    from tests.test_api import _plan

    soc = SocMeasurement(SocStatus.OK, datetime(2026, 10, 24, 11, 30, tzinfo=UTC), value=40)
    plan = _plan()
    export = ExportIntent(
        event_identity=("e", datetime(2026, 10, 24, 14, tzinfo=UTC),
                        datetime(2026, 10, 24, 15, tzinfo=UTC)),
        window_start=datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        window_end=datetime(2026, 10, 24, 15, tzinfo=UTC), planned_export_kw=1,
        dno_export_limit_kw=2, selected_slots=(), slot_export_kw=(),
    )
    state.plan = replace(
        plan, soc=soc, export_soc_needed_pct=79.0,
        export_soc_needed_at=datetime(2026, 10, 24, 14),
        charge_intent=replace(plan.charge_intent, soc=soc, export=export),
    )
    state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    status = response.json()["plan"]["status"]
    assert status["action"] is None and status["reason"] is None
    assert status["soc_reason"] == "ok"
    assert status["export_soc_needed_pct"] is None and status["export_soc_needed_at"] is None
    assert response.json()["plan"]["next_change"] is None



def _wire_module():
    """The integration's wire module, loaded by path (it imports no Home Assistant code)."""
    import importlib.util

    path = Path(__file__).parents[1] / "custom_components" / "ha_spark" / "wire.py"
    spec = importlib.util.spec_from_file_location("ha_spark_wire_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_integration_display_codes_match_the_add_on():
    from ha_spark.authority import FALLBACK_CAUSE_SOC_INTEGRITY
    from ha_spark.energy.soc_integrity import SocStatus

    wire = _wire_module()
    assert wire.SOC_STATUSES == {status.value for status in SocStatus}
    assert wire.PLAN_MODELS == {"daily", "slots"}
    assert wire.PLAN_STRATEGIES == {"deficit", "fill"}
    assert wire.FALLBACK_CAUSES == {FALLBACK_CAUSE_SOC_INTEGRITY}


async def test_unrepresentable_instants_degrade_and_the_summary_still_parses(
    state, discovery, tmp_path, monkeypatch,
):
    from dataclasses import replace
    from datetime import timedelta, timezone

    from ha_spark import authority
    from ha_spark.api.contract import AuthorityInputs, plan_summary
    from ha_spark.authority import AuthorityStore, HoldDevice, HoldRecord
    from tests.test_api import _plan

    edge = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14)))
    monkeypatch.setattr(authority, "resolve_data_dir", lambda settings: tmp_path)
    store = AuthorityStore(state.settings)
    store.initialise()
    assert store.state is not None
    device = HoldDevice(driver="solis", power_switch="switch.x", hub="h", slave=1)
    store.save(store.state.model_copy(update={
        "last_changed_at": edge,
        "holds": (HoldRecord(cause="dispatch", started_at=edge, device=device,
                             trusted_end=edge),),
    }))
    plan = replace(_plan(), export_soc_needed_pct=50.0, export_soc_needed_at=edge)
    body = plan_summary(
        api_version=1, session_id=discovery.session_id,
        built_at=datetime(2026, 10, 24, 12, tzinfo=UTC),
        loop_tick=datetime(2026, 10, 24, 11, 59, 30, tzinfo=UTC), plan=plan,
        plan_at=datetime(2026, 10, 24, 11, 30, tzinfo=UTC), settings=state.settings,
        authority=AuthorityInputs(store.state, True, False),
    )
    assert body["authority"]["last_changed_at"] is None
    assert body["authority"]["holds"]["dispatch"] == {"active": True, "trusted_end": None}
    status = body["plan"]["status"]
    assert status["export_soc_needed_pct"] is None and status["export_soc_needed_at"] is None
    summary = _wire_module().parse_summary(json.loads(json.dumps(body)))
    assert summary.authority.holds.dispatch is True


async def test_unfinished_heartbeat_body_times_out(state, discovery, monkeypatch):
    import asyncio

    monkeypatch.setattr(server, "_HEARTBEAT_BODY_TIMEOUT_S", 0.05)

    async def stalled() -> AsyncIterator[bytes]:
        yield b'{"api_version": 1,'
        await asyncio.sleep(10)
        yield b"}"

    response = await asyncio.wait_for(request(
        state, discovery, headers={"Authorization": f"Bearer {discovery.auth_token}"},
        content=stalled(),
    ), timeout=5)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid heartbeat"}


async def test_deeply_nested_session_file_nulls_v2l(state, discovery):
    state.settings = state.settings.model_copy(update={"v2l_power_entity": "sensor.v2l"})
    path = Path(state.settings.db_path).parent / "ha_spark_v2l_session.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert response.json()["v2l"] is None



@pytest.mark.parametrize("status", ["UNAVAILABLE", "MALFORMED", "NOT_FINITE"])
async def test_soc_reason_never_quotes_the_raw_state(state, discovery, status):
    from dataclasses import replace

    from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
    from tests.test_api import _plan

    soc = SocMeasurement(SocStatus[status], datetime(2026, 10, 24, 11, 30, tzinfo=UTC),
                         raw_state=f"sensor.{CANARY}")
    plan = _plan()
    state.plan = replace(plan, soc=soc, charge_intent=replace(plan.charge_intent, soc=soc))
    state.plan_at = datetime(2026, 10, 24, 11, 30, tzinfo=UTC)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    assert response.status_code == 200
    assert CANARY not in response.text
    assert response.json()["plan"]["status"]["soc_reason"].startswith("SoC ")


@pytest.mark.parametrize("hour,action,next_hour,next_action", [
    (10, "export", 11, "hold"), (12, "hold", 13, "export"), (14, "charge", 16, "idle"),
])
async def test_action_and_next_change_through_the_heartbeat(
    state, discovery, monkeypatch, hour, action, next_hour, next_action,
):
    """The resolver's overlap and half-open cases, carried through summary
    construction, the household time zone and JSON (tests/test_plan_status.py)."""
    from tests.test_plan_status import at, scenario

    monkeypatch.setattr(server, "_heartbeat_now", lambda: at(hour))
    state.settings = state.settings.model_copy(update={"timezone": "Europe/London"})
    state.plan = scenario()
    state.plan_at = at(9)
    response = await request(state, discovery,
                             headers={"Authorization": f"Bearer {discovery.auth_token}"})
    plan = response.json()["plan"]
    assert plan["status"]["action"] == action
    assert plan["next_change"]["at"] == at(next_hour).isoformat()
    assert plan["next_change"]["action"] == next_action
    assert CANARY not in response.text
