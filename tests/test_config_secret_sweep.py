"""Phase-wide canaries stay inside secret storage, never owner-facing diagnostics."""

import asyncio
import io
import json

import pytest

from ha_spark import cli
from ha_spark.api.server import AppState
from ha_spark.api.static_assets import packaged_assets
from ha_spark.config import _SECRET_OPTION_KEYS, ConfigError, Settings
from ha_spark.daemon_channel import DaemonChannel, submit_socket
from ha_spark.devices.execution import execution_context
from ha_spark.settings_service import ConfigurationRepair, SettingsService
from tests.api_helpers import owner_client
from tests.authority_helpers import seed_authority, seed_settings
from tests.test_plan_timeline import stored_run

CANARY = "SECRET-SWEEP-466-DO-NOT-ECHO"


async def test_config_secret_canary_sweep(tmp_path, monkeypatch, capsys, caplog):
    caplog.set_level("INFO")
    context = execution_context()
    monkeypatch.setattr(context, "authority", None)
    monkeypatch.setattr(context, "config_generation", context.config_generation)
    bootstrap = Settings(
        ha_url="http://ha.test", ha_token=CANARY, supervisor_token=CANARY,
        db_path=str(tmp_path / "ha_spark.db"),
    )
    secrets = dict.fromkeys(_SECRET_OPTION_KEYS, CANARY)
    service = seed_settings(tmp_path, secrets, settings=bootstrap)
    seed_authority(bootstrap, "observe")
    surfaces = [service.read_view(), service.export()]
    for changes in ({CANARY: CANARY}, {"tariff_provider": CANARY},
                    {key: {CANARY: CANARY} for key in secrets}):
        for operation in (service.save, service.import_document):
            with pytest.raises(ConfigError) as caught:
                operation(changes, service.revision, "cli")
            surfaces.append(str(caught.value))
    surfaces.append(service.save(secrets, service.revision, "cli"))
    surfaces.append(service.import_document(secrets, service.revision, "import"))
    monkeypatch.setattr(cli, "Settings", lambda: bootstrap)
    async with DaemonChannel(bootstrap) as channel:
        channel.settings_service = service
        for op, kwargs in (
            ("config_show", {}), ("config_export", {}),
            ("config_save", {"expected_revision": service.revision,
                             "changes": {CANARY: CANARY}}),
            ("config_import", {"expected_revision": service.revision,
                               "document": {"tariff_provider": CANARY}}),
        ):
            reply = await submit_socket(bootstrap, op, **kwargs)
            assert reply is not None
            surfaces.append(reply.model_dump())
        for key in secrets:
            reply = await submit_socket(
                bootstrap, "config_set_secret", expected_revision=service.revision,
                field=key, value=CANARY,
            )
            assert reply is not None and reply.error is None
            surfaces.append(reply.model_dump())
        state = AppState(bootstrap, tmp_path / "options.json", settings_service=service)
        with owner_client(state) as client:
            record = stored_run()
            state.set_plan(record.run.plan, record)
            plan_response = client.get("/api/plan")
            assert plan_response.status_code == 200
            assert plan_response.json()["timeline"] is not None
            surfaces.append(plan_response.text)
            for path in ["/", "/api/health", *(f"/static/{name}" for name in packaged_assets())]:
                response = client.get(path)
                assert response.status_code == 200
                surfaces.append(response.text)
            for response in (
                client.get("/api/config"),
                client.post("/api/config", json={"expected_revision": service.revision,
                                                "changes": secrets}),
                client.post("/api/config", json={"expected_revision": service.revision,
                                                "changes": {CANARY: CANARY}}),
                client.post("/api/config/import", json={"expected": service.revision,
                                                       "document": secrets}),
            ):
                surfaces.append(response.text)
            malformed = client.post("/api/config/import", content='{"document": "' + CANARY,
                                    headers={"Content-Type": "application/json"})
            assert malformed.status_code == 400
            surfaces.append(malformed.text)
        document = tmp_path / "import.yaml"
        document.write_text(json.dumps(secrets))
        commands = [(["config", "show"], 0), (["config", "export"], 0),
                    (["config", "set", "target_soc_cap=85"], 0),
                    (["config", "set", f"{CANARY}={CANARY}"], 1),
                    (["config", "import", str(document)], 0)]
        for key in secrets:
            commands.append((["config", "set-secret", key], 0))
        for command, expected in commands:
            # Reset stdin for each secret, including after the preceding secret consumed it.
            monkeypatch.setattr(cli.sys, "stdin", io.StringIO(CANARY + "\n"))
            assert await asyncio.to_thread(cli.main, command) == expected
            captured = capsys.readouterr()
            surfaces.append(captured.out + captured.err)
        for malformed in ('{"octopus_api_key": "' + CANARY,
                          'axle_api_key: ["' + CANARY + '"'):
            document.write_text(malformed)
            assert await asyncio.to_thread(cli.main, ["config", "import", str(document)]) == 2
            captured = capsys.readouterr()
            surfaces.append(captured.out + captured.err)
        path = tmp_path / "ha_spark_config.json"
        invalid = json.loads(path.read_text())
        invalid["settings"]["tariff_provider"] = CANARY
        path.write_text(json.dumps(invalid))
        repairing = SettingsService(tmp_path / "options.json", settings=bootstrap,
                                    data_dir=tmp_path)
        with pytest.raises(ConfigurationRepair) as caught:
            repairing.load(writable=True)
        surfaces.extend([str(caught.value), repairing.read_view()])
        channel.settings_service = repairing
        reply = await submit_socket(bootstrap, "config_show")
        assert reply is not None
        surfaces.append(reply.model_dump())
        state = AppState(bootstrap, tmp_path / "options.json", settings_service=repairing)
        with owner_client(state) as client:
            for path in ["/", *(f"/static/{name}" for name in packaged_assets())]:
                response = client.get(path)
                assert response.status_code == 200
                surfaces.append(response.text)
            surfaces.append(client.get("/api/config").text)
            health = client.get("/api/health")
            assert health.json()["status"] == "fail"
            surfaces.append(health.text)
            refused = client.post("/api/config/import", json={
                "expected": repairing.repair_token(), "document": {CANARY: CANARY},
            })
            assert refused.status_code == 400
            surfaces.append(refused.text)
        reply = await submit_socket(
            bootstrap, "config_import", repair_token=repairing.repair_token(),
            document={"tariff_provider": CANARY},
        )
        assert reply is not None and reply.error == "invalid configuration"
        surfaces.append(reply.model_dump())
        assert await asyncio.to_thread(cli.main, ["health"]) == 1
        captured = capsys.readouterr()
        surfaces.append(captured.out + captured.err)
        for command in (["config", "show"], ["config", "export"],
                        ["config", "set", "target_soc_cap=85"],
                        ["config", "set-secret", "axle_api_key"]):
            await asyncio.to_thread(cli.main, command)
            captured = capsys.readouterr()
            surfaces.append(captured.out + captured.err)
        document.write_text(json.dumps(secrets))
        assert await asyncio.to_thread(cli.main, ["config", "import", str(document)]) == 0
        assert repairing.repair is None
        captured = capsys.readouterr()
        surfaces.append(captured.out + captured.err)
    assert CANARY not in json.dumps(surfaces)
    assert CANARY not in caplog.text
