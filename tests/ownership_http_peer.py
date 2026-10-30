"""Stdio bridge to the real add-on route for the separately pinned HA harness.

Run by test_ownership_review with the repo Python on PATH. TestClient is entirely
in-process: no socket, HA client, or native transport is used.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ha_spark.api.server import AppState, build_app
from ha_spark.authority import AuthorityStore
from ha_spark.config import Settings
from ha_spark.devices.execution import execution_context
from ha_spark.integration_discovery import IntegrationDiscovery


def main() -> None:
    request = json.load(sys.stdin)
    snapshot = request["snapshot"]
    now = datetime.fromisoformat(snapshot["sent_at"])
    with tempfile.TemporaryDirectory() as directory, patch(
        "ha_spark.authority.resolve_data_dir", return_value=Path(directory),
    ):
        path = Path(directory)
        settings = Settings(supervisor_token="unused", db_path=str(path / "db"))
        store = AuthorityStore(settings)
        store.initialise()
        assert store.state is not None
        store.save(store.state.model_copy(update={"committed_mode": request["mode"]}))
        execution_context().authority = store
        state = AppState(settings, path / "options.json")
        state.loop_tick = now
        discovery = IntegrationDiscovery(
            supervisor_token="unused", host="addon", auth_token="test",
        )
        discovery.session_id = request["session_id"]
        with patch("ha_spark.api.server._heartbeat_now", return_value=now):
            app = build_app(state, ingress_peer="proxy", integration=discovery)
            with TestClient(app) as client:
                response = client.post(
                    "/integration/heartbeat", json=snapshot,
                    headers={"Authorization": "Bearer test"},
                )
        print(json.dumps({"status": response.status_code, "body": response.json(),
                          "known": store.known}))


if __name__ == "__main__":
    main()
