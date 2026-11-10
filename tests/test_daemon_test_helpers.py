"""The daemon rig refuses even network errors swallowed by forecast fallbacks."""
import socket

import pytest
import respx

from tests.test_daemon_executor import running_daemon, settings_at


@pytest.mark.parametrize("operation", ["dns", "connect", "connect_ex"])
@respx.mock
async def test_rig_reports_swallowed_network_attempts(tmp_path, monkeypatch, operation):
    settings = settings_at(tmp_path)
    respx.get(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    respx.post(url__regex=r"http://ha.test/api/.*").respond(200, json=[])
    with pytest.raises(AssertionError, match="Unstubbed network calls"):
        async with running_daemon(settings, tmp_path, monkeypatch):
            # Simulate a consumer catching the network exception and using a
            # baseline instead. The rig must still report the attempted access.
            with pytest.raises(AssertionError, match="DNS lookup|Internet socket"):
                if operation == "dns":
                    socket.getaddrinfo("ha.test", 80)
                else:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                        getattr(sock, operation)(("192.0.2.1", 80))
