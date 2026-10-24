"""Discovery's public lifecycle talks only to Supervisor, without leaking secrets."""

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path

import httpx
import pytest
import respx

from ha_spark import authority, daemon_channel, integration_discovery, settings_service
from ha_spark.config import Settings
from ha_spark.energy import scheduler
from ha_spark.integration_discovery import IntegrationDiscovery
from ha_spark.logging import setup_logging
from tests.test_scheduler import _plan

FIXTURE = json.loads((Path(__file__).parent / "fixtures/contract/discovery.json").read_text())
DISCOVERY_URL = "http://supervisor/discovery"
MESSAGE_ID = "a" * 32


def pairing() -> IntegrationDiscovery:
    return IntegrationDiscovery(
        supervisor_token="supervisor-secret", host=FIXTURE["host"],
        auth_token=FIXTURE["auth_token"], session_id=FIXTURE["session_id"],
    )


async def wait_for_post(route: respx.Route) -> None:
    async with asyncio.timeout(2):
        while not route.called:  # noqa: ASYNC110 - wait for respx's recorded public call
            await asyncio.sleep(0)
        # Let the transport's response complete before cancelling the announcer.
        await asyncio.sleep(0)


@respx.mock
async def test_announce_and_withdraw_match_contract() -> None:
    post = respx.post(DISCOVERY_URL).respond(
        200, json={"result": "ok", "data": {"uuid": MESSAGE_ID}}
    )
    delete = respx.delete(f"{DISCOVERY_URL}/{MESSAGE_ID}").respond(200)
    link = pairing()
    link.start()
    link.start()  # repair -> normal is the same boot, not another announcement
    await wait_for_post(post)
    await link.cancel_announcement()
    await link.withdraw()
    assert post.call_count == 1
    assert json.loads(post.calls[0].request.content) == {"service": "ha_spark", "config": FIXTURE}
    assert post.calls[0].request.headers["Authorization"] == "Bearer supervisor-secret"
    assert delete.call_count == 1


@respx.mock
async def test_retry_and_failures_never_log_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    canary = "DISCOVERY-SECRET-CANARY"
    responses = [
        httpx.Response(500, text=canary, extensions={"reason_phrase": canary.encode()}),
        httpx.ConnectError(canary),
        httpx.Response(200, json={"result": "ok", "data": {"uuid": MESSAGE_ID}}),
    ]
    post = respx.post(DISCOVERY_URL).mock(side_effect=responses)
    respx.delete(f"{DISCOVERY_URL}/{MESSAGE_ID}").mock(side_effect=httpx.ConnectError(canary))
    link = pairing()
    link.start()
    async with asyncio.timeout(5):
        while post.call_count < 3:  # noqa: ASYNC110 - await the HTTP retry, not internals
            await asyncio.sleep(0.01)
    await link.cancel_announcement()
    await link.withdraw()
    assert canary not in caplog.text
    assert FIXTURE["auth_token"] not in caplog.text
    assert "supervisor-secret" not in caplog.text


async def test_status_line_of_a_real_supervisor_reply_never_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """respx bypasses httpcore, whose DEBUG trace logs status lines: use a socket."""
    canary = "DISCOVERY-REASON-CANARY"
    replied = asyncio.Event()

    async def supervisor(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            f"HTTP/1.1 500 {canary}\r\nX-Echo: {canary}\r\nContent-Length: 0\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()
        replied.set()

    server = await asyncio.start_server(supervisor, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(integration_discovery, "_DISCOVERY_URL", f"http://127.0.0.1:{port}/d")
    root_level, httpcore_level = logging.getLogger().level, logging.getLogger("httpcore").level
    monkeypatch.setattr("ha_spark.logging._CONFIGURED", True)
    setup_logging("WARNING")
    caplog.set_level(logging.DEBUG)
    link = pairing()
    try:
        link.start()
        async with asyncio.timeout(5):
            await replied.wait()
            while "failed" not in caplog.text:  # noqa: ASYNC110 - wait for the logged type
                await asyncio.sleep(0.01)
        await link.cancel_announcement()
    finally:
        server.close()
        logging.getLogger("httpcore").setLevel(httpcore_level)
        logging.getLogger().setLevel(root_level)
    assert canary not in caplog.text
    assert "X-Echo" not in caplog.text and "HTTP/1.1" not in caplog.text
    assert FIXTURE["auth_token"] not in caplog.text
    failures = [r.getMessage() for r in caplog.records if "discovery" in r.getMessage()]
    assert failures and set(failures) == {"Supervisor discovery failed: ValueError"}


@respx.mock
async def test_cancel_joins_an_inflight_post_and_nothing_posts_after() -> None:
    """Cancelling joins the post in flight. Its reply never arrived, so there is no
    message id and nothing to withdraw: a post the Supervisor accepted without
    replying stays until the next boot replaces it (spec #472, accepted)."""
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked(request: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return httpx.Response(200, json={"result": "ok", "data": {"uuid": MESSAGE_ID}})

    respx.post(DISCOVERY_URL).mock(side_effect=blocked)
    link = pairing()
    link.start()
    await entered.wait()
    await link.cancel_announcement()
    assert cancelled.is_set()
    await link.withdraw()
    await asyncio.sleep(0)
    assert entered.is_set()
    assert len(respx.calls) == 0  # cancelled calls aren't recorded; no DELETE or late POST


@respx.mock
async def test_withdrawal_is_bounded() -> None:
    post = respx.post(DISCOVERY_URL).respond(
        200, json={"result": "ok", "data": {"uuid": MESSAGE_ID}}
    )

    async def blocked(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)

    respx.delete(f"{DISCOVERY_URL}/{MESSAGE_ID}").mock(side_effect=blocked)
    link = pairing()
    link.start()
    await wait_for_post(post)
    await link.cancel_announcement()
    async with asyncio.timeout(4):
        await link.withdraw()


@respx.mock
@pytest.mark.parametrize("body", [
    {"result": "error", "data": {"uuid": MESSAGE_ID}},
    {"result": "ok", "data": {"uuid": "../../unsafe"}},
    {"result": "ok", "data": []},
])
async def test_invalid_supervisor_reply_is_not_used_for_withdrawal(body: object) -> None:
    post = respx.post(DISCOVERY_URL).respond(200, json=body)
    link = pairing()
    link.start()
    await wait_for_post(post)
    await link.cancel_announcement()
    await link.withdraw()
    assert len(respx.calls) == 1


def test_boot_identity_is_fresh_and_not_in_repr() -> None:
    one = IntegrationDiscovery(supervisor_token="secret", host="host")
    two = IntegrationDiscovery(supervisor_token="secret", host="host")
    assert one.auth_token != two.auth_token
    assert one.session_id != two.session_id
    assert len(one.auth_token) >= 43
    assert one.auth_token not in repr(one)
    assert one.session_id not in repr(one)


@respx.mock
@pytest.mark.parametrize("repair,standalone,handover", [
    (False, False, False), (True, False, False), (False, True, False), (True, False, True),
])
async def test_daemon_pairing_shutdown_order_and_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair: bool, standalone: bool,
    handover: bool,
) -> None:
    """The daemon announces only after serving and withdraws after hardware work."""
    for module in (authority, daemon_channel, settings_service):
        monkeypatch.setattr(module, "resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(scheduler, "OPTIONS_PATH", tmp_path / "options.json")
    if repair:
        (tmp_path / "options.json").write_text('{"tariff_provider": "invalid"}')
    settings = Settings(
        ha_url="http://ha.test" if standalone else "",
        ha_token="ha-secret" if standalone else "", supervisor_token="supervisor-secret",
        db_path=str(tmp_path / "ha_spark.db"),
    )
    events: list[str] = []
    announced = asyncio.Event()
    ticked = asyncio.Event()

    def post(request: httpx.Request) -> httpx.Response:
        assert "serving" in events
        events.append("announce")
        announced.set()
        return httpx.Response(200, json={"result": "ok", "data": {"uuid": MESSAGE_ID}})

    def delete(request: httpx.Request) -> httpx.Response:
        events.append("withdraw")
        return httpx.Response(200)

    post_route = respx.post(DISCOVERY_URL).mock(side_effect=post)
    delete_route = respx.delete(f"{DISCOVERY_URL}/{MESSAGE_ID}").mock(side_effect=delete)
    respx.route().respond(200, json={})

    async def serve(server: object) -> asyncio.Task[None]:
        events.append("serving")
        return asyncio.create_task(asyncio.sleep(0))

    async def stop(server: object, task: asyncio.Task[None]) -> None:
        events.append("listener stopped")
        await task

    async def run_once(*args: object, **kwargs: object) -> scheduler.RunResult:
        plan = _plan()
        return scheduler.RunResult(plan, plan)

    async def sample(*args: object) -> None:
        ticked.set()
        await asyncio.Event().wait()

    class Device:
        async def write_safe_state(self) -> list[str]:
            assert not delete_route.called
            events.append("safe state")
            return []

    monkeypatch.setattr(scheduler, "make_server", lambda *a: object())
    monkeypatch.setattr(scheduler, "serve_in_background", serve)
    monkeypatch.setattr(scheduler, "stop_server", stop)
    monkeypatch.setattr(scheduler, "run_once", run_once)
    monkeypatch.setattr(scheduler, "sample_signals", sample)
    monkeypatch.setattr(scheduler, "_real_write_mode", lambda *a: True)
    monkeypatch.setattr(scheduler, "inverter_device", lambda *a: Device())
    task = asyncio.create_task(scheduler.run_forever(settings, poll_seconds=0))
    try:
        async with asyncio.timeout(5):
            if not standalone:
                await announced.wait()
            if handover:
                shown = await daemon_channel.submit_socket(settings, "config_show")
                assert shown is not None and shown.config is not None
                imported = await daemon_channel.submit_socket(
                    settings, "config_import", repair_token=shown.config["repair_token"],
                    document={},
                )
                assert imported is not None and imported.error is None
            if not repair or handover:
                await ticked.wait()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if standalone:
        assert not any(call.request.url.host == "supervisor" for call in respx.calls)
    else:
        assert post_route.call_count == delete_route.call_count == 1
        assert events[-1] == "withdraw"
        if not repair or handover:
            assert events.index("safe state") < events.index("withdraw")
        assert events.count("serving") == (2 if handover else 1)
    # Discovery credentials have not been written alongside the daemon's stores.
    if post_route.called:
        config = json.loads(post_route.calls[0].request.content)["config"]
        for path in tmp_path.iterdir():  # noqa: ASYNC240 - tiny isolated test directory
            if path.is_file():
                content = path.read_bytes()
                assert config["auth_token"].encode() not in content
                assert config["session_id"].encode() not in content
