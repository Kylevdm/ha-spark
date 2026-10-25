"""Configuration repair: an untrusted store keeps ha-spark reachable and inert (#463)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ha_spark import cli
from ha_spark.api.server import AppState, build_app
from ha_spark.authority import HoldDevice, HoldRecord, Transition
from ha_spark.config import ConfigError, Settings
from ha_spark.daemon_channel import submit_socket
from ha_spark.devices.execution import execution_context
from ha_spark.energy import scheduler
from ha_spark.energy.scheduler import ReconcileResult
from ha_spark.settings_service import ConfigurationRepair, RepairTokenConflict, SettingsService
from tests.api_helpers import OWNER_CONFIG_HEADERS, owner_client
from tests.authority_helpers import seed_authority, seed_settings, settings_bytes

CANARY = "CANARY-SECRET-463"


def bootstrap(path: Path) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="test", db_path=str(path / "ha_spark.db"))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Path]:
    """A committed valid pair with a fallback schedule and a secret, then left on disk."""
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    seed_settings(tmp_path, {
        "solis_fallback_current_a": 20, "inverter_power_switch_entity": "select.old",
        "octopus_api_key": CANARY,
    }, settings=bootstrap(tmp_path))
    try:
        yield tmp_path
    finally:
        context.authority, context.config_generation = authority, generation


def corrupt(path: Path, **settings: object) -> None:
    document = json.loads((path / "ha_spark_config.json").read_text())
    document["settings"].update(settings)
    (path / "ha_spark_config.json").write_text(json.dumps(document))


def authority_bytes(path: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in path.glob("authority*")}


def daemon_service(path: Path) -> SettingsService:
    return SettingsService(path / "options.json", settings=bootstrap(path), data_dir=path)


def test_invalid_store_enters_repair_naming_fields_only(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    before = settings_bytes(store)
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair) as caught:
        service.load(writable=True)
    assert "tariff_provider" in str(caught.value)
    assert CANARY not in str(caught.value)
    view = service.read_view()
    assert view["status"] == "configuration_repair"
    assert any("tariff_provider" in reason for reason in view["reasons"])
    assert len(view["repair_token"]) == 64
    assert CANARY not in json.dumps(view)
    with pytest.raises(ConfigError):
        service.save({"notify_service": "x"}, 0, "cli")
    assert settings_bytes(store) == before


def test_missing_store_after_marker_never_reseeds_or_defaults(store: Path) -> None:
    for name in ("ha_spark_config.json", "ha_spark_secrets.json"):
        (store / name).unlink()
    (store / "options.json").write_text(json.dumps({"target_soc_cap": 85}))
    before = {p.name: p.read_bytes() for p in store.iterdir() if p.is_file()}
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair, match="unreadable"):
        service.load(writable=True)
    assert {p.name: p.read_bytes() for p in store.iterdir() if p.is_file()} == before
    assert service.read_view()["status"] == "configuration_repair"


def test_failed_seed_enters_repair_and_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text(json.dumps({"tariff_provider": CANARY}))
    service = daemon_service(tmp_path)
    with pytest.raises(ConfigurationRepair) as caught:
        service.load(writable=True)
    assert CANARY not in str(caught.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["options.json"]
    assert service.read_view()["repair_token"] == "none"


def test_repair_import_needs_the_current_token(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    before = settings_bytes(store)
    for stale in ("none", 3, service.repair_token()[::-1]):
        with pytest.raises(RepairTokenConflict):
            service.import_document({"target_soc_cap": 85}, stale, "import")
    assert settings_bytes(store) == before
    assert service.read_view()["status"] == "configuration_repair"


def test_repair_import_commits_and_preserves_readable_secrets(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    result = service.import_document(
        {"target_soc_cap": 85, "solis_fallback_current_a": 20}, service.repair_token(), "import",
    )
    assert result["status"] == "live"
    assert service.repair is None
    fresh = daemon_service(store)
    settings = fresh.load(writable=True)
    assert settings.target_soc_cap == 85
    assert settings.octopus_api_key == CANARY
    assert fresh.export() == {"target_soc_cap": 85, "solis_fallback_current_a": 20}


def test_newer_schema_document_is_never_overwritten(store: Path) -> None:
    document = json.loads((store / "ha_spark_config.json").read_text())
    document["schema_version"] = 2
    (store / "ha_spark_config.json").write_text(json.dumps(document))
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair, match="schema_version"):
        service.load(writable=True)
    before = settings_bytes(store)
    with pytest.raises(ConfigError, match="newer"):
        service.import_document({}, service.repair_token(), "import")
    assert settings_bytes(store) == before


def test_read_only_load_cannot_import_in_repair(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair):
        service.load()
    before = settings_bytes(store)
    with pytest.raises(ConfigError, match="daemon"):
        service.import_document({}, service.repair_token(), "import")
    assert settings_bytes(store) == before


@pytest.mark.parametrize("authority", ["optimise", "transition", "unknown"])
def test_repair_import_keeps_the_optimise_fallback_refusal(store: Path, authority: str) -> None:
    corrupt(store, tariff_provider=CANARY)
    authority_store = seed_authority(
        bootstrap(store), "optimise" if authority == "optimise" else "observe",
    )
    if authority == "transition":
        authority_store.save(authority_store.state.model_copy(update={
            "transition": Transition(target="optimise", started_at=datetime.now(UTC)),
        }))
    elif authority == "unknown":
        authority_store.store_path.unlink()
    execution_context().authority = None
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    authority_before = authority_bytes(store)
    before = settings_bytes(store)
    if authority == "unknown":
        # In repair there is no trusted current fallback to protect.
        service.import_document({}, service.repair_token(), "import")
        assert service.repair is None
    else:
        with pytest.raises(ConfigError, match="solis_fallback_current_a"):
            service.import_document({}, service.repair_token(), "import")
        assert settings_bytes(store) == before
        assert service.repair is not None
    assert authority_bytes(store) == authority_before


# --- the daemon in repair, at the `run` entry point -------------------------------------------


def seed_optimise_with_hold_and_transition(path: Path) -> None:
    store = seed_authority(bootstrap(path), "optimise")
    now = datetime.now(UTC)
    store.save(store.state.model_copy(update={
        "holds": (HoldRecord(
            cause="ev", started_at=now, confirmed=True,
            device=HoldDevice(driver="solis", power_switch="select.old", hub="hub", slave=1),
        ),),
        "transition": Transition(target="protect", started_at=now, steps=("release_holds",)),
    }))
    execution_context().authority = None


@asynccontextmanager
async def repairing_daemon(path: Path, monkeypatch: pytest.MonkeyPatch):
    """`run_forever` on an invalid store; every actuating seam records instead of acting."""
    for name in ("SUPERVISOR_TOKEN",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HA_URL", "http://ha.test")
    monkeypatch.setenv("HA_TOKEN", "test")
    monkeypatch.setenv("DB_PATH", str(path / "ha_spark.db"))
    events: list[str] = []
    states: list[AppState] = []
    planned = asyncio.Event()

    def record(name: str, result: object = None):
        async def recorder(*args: object, **kwargs: object) -> object:
            events.append(name)
            if name == "run_once":
                planned.set()
                raise RuntimeError("stop after the first plan")
            return result
        return recorder

    def capture_app(state: AppState, **kwargs: object) -> object:
        states.append(state)
        return object()

    async def serve(server: object) -> asyncio.Task[None]:
        return asyncio.create_task(asyncio.sleep(0))

    async def stop(server: object, task: asyncio.Task[None]) -> None:
        await task

    def monitor(settings: Settings) -> object:
        events.append("monitor")
        return SimpleNamespace(
            fallback_active=False, fallback_confirmed=False, recovery_ready=False,
            recovery_blocked=False, awaiting_fresh_observation=False,
            invalidate_fallback_confirmation=lambda: None,
        )

    def device(*args: object) -> object:
        events.append("inverter_device")
        raise AssertionError("no device in repair")

    monkeypatch.setattr(scheduler, "OPTIONS_PATH", path / "options.json")
    monkeypatch.setattr(scheduler, "build_app", capture_app)
    monkeypatch.setattr(scheduler, "make_server", lambda *args: object())
    monkeypatch.setattr(scheduler, "serve_in_background", serve)
    monkeypatch.setattr(scheduler, "stop_server", stop)
    monkeypatch.setattr(scheduler, "inverter_device", device)
    monkeypatch.setattr(scheduler.SocMonitor, "load", monitor)
    monkeypatch.setattr(scheduler, "soc_monitor_tick", record("soc_monitor_tick"))
    monkeypatch.setattr(scheduler, "reconcile_tick",
                        record("reconcile", ReconcileResult([], None)))
    monkeypatch.setattr(scheduler, "_mode_lifecycle", record("lifecycle"))
    monkeypatch.setattr(scheduler, "run_once", record("run_once"))
    for name in ("sample_signals", "record_slot", "record_closed_days",
                 "readiness_tick", "notify_remapped_holds"):
        monkeypatch.setattr(scheduler, name, record(name))
    settings = Settings()
    task = asyncio.create_task(scheduler.run_forever(settings, poll_seconds=0))
    try:
        for _ in range(200):
            reply = await submit_socket(settings, "authority_status")
            if reply is not None:
                break
            await asyncio.sleep(0.01)
        yield settings, events, states, planned
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@respx.mock
async def test_repair_serves_only_the_fix_and_never_actuates(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    corrupt(store, tariff_provider=CANARY)
    seed_optimise_with_hold_and_transition(store)
    authority_before, config_before = authority_bytes(store), settings_bytes(store)
    # No real route: any HTTP request at all (HA, device, supplier) is recorded here.
    outbound = respx.route().mock(return_value=httpx.Response(500))
    async with repairing_daemon(store, monkeypatch) as (settings, events, states, _):
        for op in ("apply", "replan", "sync_clock"):
            reply = await submit_socket(settings, op)
            assert reply is not None and reply.error == "configuration_repair"
        for target in ("protect", "optimise"):
            reply = await submit_socket(settings, "mode", target=target, expected_epoch=1)
            assert reply is not None and reply.error == "configuration_repair"
        for op, kwargs in (("config_export", {}),
                           ("config_save", {"expected_revision": 0, "changes": {}}),
                           ("config_set_secret", {"expected_revision": 0,
                                                  "field": "axle_api_key", "value": CANARY})):
            reply = await submit_socket(settings, op, **kwargs)
            assert reply is not None and reply.error == "configuration_repair"
        status = await submit_socket(settings, "authority_status")
        assert status.authority["suspended"] == "configuration_repair"
        assert status.authority["mode"] == "optimise"
        assert len(status.authority["holds"]) == 1
        assert status.authority["transition_target"] == "protect"
        shown = await submit_socket(settings, "config_show")
        assert shown.config["status"] == "configuration_repair"
        assert CANARY not in shown.model_dump_json()

        assert await asyncio.to_thread(cli.main, ["mode", "confirm-observe"]) == 1
        assert "configuration_repair" in capsys.readouterr().out
        assert await asyncio.to_thread(cli.main, ["health"]) == 1
        out = capsys.readouterr().out
        assert "Configuration" in out and "tariff_provider" in out and CANARY not in out
        assert await asyncio.to_thread(cli.main, ["mode", "show"]) == 2
        captured = capsys.readouterr()
        view = json.loads(captured.out)
        assert view["suspended"] == "configuration_repair"
        assert view["transition"]["target"] == "protect" and len(view["holds"]) == 1
        assert CANARY not in captured.out + captured.err
        assert await asyncio.to_thread(cli.main, ["readiness", "show"]) == 2
        assert "configuration repair" in capsys.readouterr().err.lower()

        with owner_client(states[0]) as client:
            mode = client.get("/api/mode")
            assert mode.status_code == 200
            assert mode.json()["suspended"] == "configuration_repair"
            assert len(mode.json()["holds"]) == 1
            health = client.get("/api/health")
            assert health.status_code == 200 and health.json()["status"] == "fail"
            assert CANARY not in health.text
            config = client.get("/api/config")
            assert config.json()["status"] == "configuration_repair"
            assert client.post("/api/config", json={"expected_revision": 1,
                                                    "changes": {}}).status_code == 503
            assert client.post("/api/mode", json={"target": "observe", "expected_epoch": None},
                               headers={"X-HA-Spark-Request": "mode"}).status_code == 503
            assert client.get("/api/plan").status_code == 503
            assert client.post("/agent/run").status_code == 503
    assert events == []
    assert not outbound.called
    assert authority_bytes(store) == authority_before
    assert settings_bytes(store) == config_before


@respx.mock
async def test_import_ends_repair_and_reconciles_holds_before_the_first_plan(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    corrupt(store, tariff_provider=CANARY)
    seed_optimise_with_hold_and_transition(store)
    document = store / "fixed.json"
    document.write_text(json.dumps({"solis_fallback_current_a": 20,
                                    "inverter_power_switch_entity": "select.old"}))
    outbound = respx.route().mock(return_value=httpx.Response(500))
    async with repairing_daemon(store, monkeypatch) as (settings, events, states, planned):
        assert events == [] and not outbound.called
        assert await asyncio.to_thread(cli.main, ["config", "import", str(document)]) == 0
        await asyncio.wait_for(planned.wait(), 5)
    assert events.index("monitor") < events.index("reconcile") < events.index("run_once")
    assert events.index("reconcile") < events.index("lifecycle")
    assert CANARY not in capsys.readouterr().out


@respx.mock
async def test_repair_builds_and_serves_only_the_ingress_listener(
    store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt(store, tariff_provider=CANARY, agent_surface="on", agent_expose_port=True)
    apps, servers, served = [], [], []
    ingress_server = scheduler._ingress_server

    def capture_ingress(state):
        # The daemon fixture installs its server fakes before entering repair.
        build, make = scheduler.build_app, scheduler.make_server
        serve = scheduler.serve_in_background

        def capture_build(*args, **kwargs):
            apps.append(kwargs)
            return build(*args, **kwargs)

        def capture_server(app, host, port):
            server = make(app, host, port)
            servers.append((server, host, port))
            return server

        async def capture_serve(server):
            served.append(server)
            return await serve(server)

        monkeypatch.setattr(scheduler, "build_app", capture_build)
        monkeypatch.setattr(scheduler, "make_server", capture_server)
        monkeypatch.setattr(scheduler, "serve_in_background", capture_serve)
        return ingress_server(state)

    monkeypatch.setattr(scheduler, "_ingress_server", capture_ingress)
    async with repairing_daemon(store, monkeypatch):
        assert apps == [{}]
        assert len(servers) == 1
        server, host, port = servers[0]
        assert (host, port) == ("127.0.0.1", scheduler.INGRESS_PORT)
        assert port != scheduler.AGENT_PORT
        assert served == [server]
    assert len(apps) == len(servers) == len(served) == 1
    assert not respx.calls


@respx.mock
async def test_running_repair_ingress_import_resumes_normal_startup(
    store: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corrupt(store, tariff_provider=CANARY)
    seed_optimise_with_hold_and_transition(store)
    async with repairing_daemon(store, monkeypatch) as (_, events, states, planned):
        def import_from_ingress():
            with owner_client(states[0]) as client:
                view = client.get("/api/config").json()
                return client.post("/api/config/import", json={
                    "expected": view["repair_token"],
                    "document": {"solis_fallback_current_a": 20,
                                 "inverter_power_switch_entity": "select.old"},
                })

        response = await asyncio.to_thread(import_from_ingress)
        assert response.status_code == 200, response.text
        assert CANARY not in response.text
        await asyncio.wait_for(planned.wait(), 5)
        assert states[0].settings_service.repair is None
        assert len(states) == 2
        assert events.index("monitor") < events.index("reconcile") < events.index("run_once")
        assert events.index("reconcile") < events.index("lifecycle")
    assert not respx.calls


def test_repair_health_reports_configuration_field_reasons_without_secrets(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    with owner_client(repair_state(store)) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "fail"
    assert any("tariff_provider" in reason
               for reason in response.json()["configuration"]["reasons"])
    assert CANARY not in response.text


@pytest.mark.parametrize("command", ["coverage", "slots"])
def test_readiness_reports_repair_for_coverage_and_slots(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str,
) -> None:
    corrupt(store, tariff_provider=CANARY)
    monkeypatch.setenv("HA_URL", "http://ha.test")
    monkeypatch.setenv("HA_TOKEN", "test")
    monkeypatch.setenv("DB_PATH", str(store / "ha_spark.db"))
    assert cli.main(["readiness", command]) == 2
    captured = capsys.readouterr()
    assert "configuration repair" in captured.err.lower()
    assert CANARY not in captured.out + captured.err


# --- ingress -----------------------------------------------------------------------------------


def repair_state(path: Path) -> AppState:
    service = daemon_service(path)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    return AppState(service.bootstrap, path / "options.json", settings_service=service)


def test_ingress_import_with_the_repair_token_ends_repair(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    state = repair_state(store)
    with owner_client(state) as client:
        view = client.get("/api/config").json()
        assert view["status"] == "configuration_repair" and CANARY not in json.dumps(view)
        stale = client.post("/api/config/import", json={"expected": 1, "document": {}})
        assert stale.status_code == 409
        refused = client.post("/api/config/import", json={
            "expected": view["repair_token"], "document": {"tariff_provider": CANARY},
        })
        assert refused.status_code == 400 and CANARY not in refused.text
        assert client.post("/api/config/import", json={"expected": view["repair_token"],
                                                       "document": {}},
                           headers={"X-HA-Spark-Request": "mode"}).status_code == 403
        imported = client.post("/api/config/import", json={
            "expected": view["repair_token"], "document": {"solis_fallback_current_a": 20},
        })
        assert imported.status_code == 200, imported.text
        assert client.get("/api/config").json()["status"] == "live"
    assert state.settings_service.repair is None


def test_ingress_import_outside_repair_uses_the_revision(store: Path) -> None:
    service = daemon_service(store)
    service.load(writable=True)
    state = AppState(service.settings, store / "options.json", settings_service=service)
    with owner_client(state) as client:
        conflict = client.post("/api/config/import",
                               json={"expected": "none", "document": {}})
        assert conflict.status_code == 409
        imported = client.post("/api/config/import", json={
            "expected": service.revision,
            "document": {"solis_fallback_current_a": 20, "target_soc_cap": 85},
        })
        assert imported.status_code == 200, imported.text
    assert service.settings.target_soc_cap == 85
    assert service.settings.octopus_api_key == CANARY


def test_import_route_is_never_on_the_published_port(store: Path) -> None:
    corrupt(store, tariff_provider=CANARY)
    state = repair_state(store)
    app = build_app(state, require_token=True, token="agent-token")
    with TestClient(app, headers={**OWNER_CONFIG_HEADERS,
                                  "Authorization": "Bearer agent-token"}) as client:
        response = client.post("/api/config/import", json={
            "expected": state.settings_service.repair_token(), "document": {},
        })
    assert response.status_code == 404
    assert state.settings_service.repair is not None


def test_pending_restart_failing_the_fallback_check_enters_repair(store: Path) -> None:
    corrupt(store, solis_fallback_current_a=None)
    (store / "ha_spark_config.pending").write_text("{}")
    seed_authority(bootstrap(store), "optimise")
    execution_context().authority = None
    before = {p.name: p.read_bytes() for p in store.iterdir() if p.is_file()}
    service = daemon_service(store)
    with pytest.raises(ConfigurationRepair, match="solis_fallback_current_a"):
        service.load(writable=True)
    assert {p.name: p.read_bytes() for p in store.iterdir() if p.is_file()} == before


# --- Batch R safety review findings ------------------------------------------


def test_non_ascii_repair_token_is_a_conflict_not_a_crash(store):
    corrupt(store, tariff_provider="invalid")
    service = SettingsService(store / "options.json", settings=bootstrap(store), data_dir=store)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    with pytest.raises(RepairTokenConflict):
        service.import_document({"min_soc": 20}, "é", "import")


def test_repair_import_fences_before_publishing(store):
    corrupt(store, tariff_provider="invalid")
    service = SettingsService(store / "options.json", settings=bootstrap(store), data_dir=store)
    with pytest.raises(ConfigurationRepair):
        service.load(writable=True)
    context = execution_context()
    generation = context.config_generation
    service.import_document(
        {"solis_fallback_current_a": 20}, service.repair_token(), "import",
    )
    assert context.config_generation == generation + 1


@pytest.mark.parametrize("command", ["health", "mode", "readiness"])
def test_diagnostics_report_a_daemon_in_repair_the_local_load_misses(
    store, monkeypatch, capsys, command,
):
    # The store loads read-only, but the running daemon is in repair.
    monkeypatch.setenv("HA_URL", "http://ha.test")
    monkeypatch.setenv("HA_TOKEN", "test")
    monkeypatch.setenv("DB_PATH", str(store / "ha_spark.db"))

    async def submit(settings, op, **kwargs):
        if op == "authority_status":
            return cli.Reply(authority={"mode": "optimise", "suspended": "configuration_repair"})
        assert op == "config_show"
        return cli.Reply(config={"status": "configuration_repair",
                                 "reasons": ["tariff_provider: invalid"]})

    monkeypatch.setattr(cli, "submit_socket", submit)
    argv = {"health": ["health"], "mode": ["mode", "show"], "readiness": ["readiness", "show"]}
    code = cli.main(argv[command])
    out = capsys.readouterr()
    assert code != 0
    assert "tariff_provider" in out.out + out.err


@pytest.mark.parametrize("argv", [
    ["config", "set", "min_soc=20", f"--{CANARY}"],
    ["config", "import", "backup.yaml", CANARY],
])
def test_config_parse_errors_never_echo_arguments(argv, capsys):
    assert cli.main(argv) == 2
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err


@pytest.fixture
def restore_context() -> Iterator[None]:
    context = execution_context()
    authority, generation = context.authority, context.config_generation
    try:
        yield
    finally:
        context.authority, context.config_generation = authority, generation


@pytest.mark.parametrize("authority", ["protect", "optimise", "to-protect", "to-optimise"])
def test_defaults_seed_past_observe_enters_repair_until_an_import(
    tmp_path: Path, restore_context: None, authority: str,
) -> None:
    store = seed_authority(
        bootstrap(tmp_path), "observe" if authority.startswith("to-") else authority,
    )
    if authority.startswith("to-"):
        store.save(store.state.model_copy(update={
            "transition": Transition(target=authority[3:], started_at=datetime.now(UTC)),
        }))
    (tmp_path / "options.json").write_text(json.dumps({"log_level": "INFO"}))
    authority_before = authority_bytes(tmp_path)
    service = daemon_service(tmp_path)
    with pytest.raises(ConfigurationRepair, match="not Observe"):
        service.load(writable=True)
    assert not list(tmp_path.glob("ha_spark_config*"))
    assert not list(tmp_path.glob("ha_spark_secrets*"))
    assert authority_bytes(tmp_path) == authority_before
    document = {"solis_fallback_current_a": 20} if authority.endswith("optimise") else {}
    service.import_document(document, service.repair_token(), "import")
    assert service.repair is None
    assert (tmp_path / "ha_spark_config.initialised").is_file()
    assert authority_bytes(tmp_path) == authority_before


@pytest.mark.parametrize("authority", ["none", "observe", "unknown"])
def test_defaults_seed_proceeds_while_authority_writes_nothing(
    tmp_path: Path, restore_context: None, authority: str,
) -> None:
    if authority != "none":
        store = seed_authority(bootstrap(tmp_path), "observe")
        if authority == "unknown":
            store.store_path.unlink()
    (tmp_path / "options.json").write_text(json.dumps({"log_level": "INFO"}))
    service = daemon_service(tmp_path)
    service.load(writable=True)
    assert service.repair is None
    assert (tmp_path / "ha_spark_config.initialised").is_file()


def test_legacy_seed_under_optimise_is_unchanged(tmp_path: Path, restore_context: None) -> None:
    seed_authority(bootstrap(tmp_path), "optimise")
    (tmp_path / "options.json").write_text(json.dumps({"min_soc": 25}))
    assert daemon_service(tmp_path).load(writable=True).min_soc == 25
