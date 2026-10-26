"""The local execution channel grants no authority and owns no device work."""
import asyncio
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ha_spark import daemon_channel
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply, Request, submit_socket


def settings_at(path: Path) -> Settings:
    return Settings(ha_url="http://ha.test", ha_token="secret", db_path=str(path / "t.db"))


async def test_settings_replan_is_best_effort_normal_queued_work(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        channel = DaemonChannel(settings_at(tmp_path))
        channel.request_replan()
        assert channel.next_request() is None
        async with channel:
            channel.request_replan()
            pending = channel.next_request()
            assert pending is not None
            assert pending.request == Request(op="replan")
            assert "requester=settings" in caplog.text
            pending.future.set_result(Reply())
            for _ in range(channel.queue.maxsize + 1):
                channel.request_replan()
            # Close-together replans join the queued one instead of filling the queue.
            assert channel.queue.qsize() == 1
            joined = channel.next_request()
            assert joined is not None and joined.request == Request(op="replan")
            joined.future.set_result(Reply())
            assert channel.next_request() is None


async def test_replan_burst_combines_into_one_run_and_shared_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO"):
        async with DaemonChannel(settings_at(tmp_path)) as channel:
            callers = [asyncio.create_task(channel.submit("replan", ingress_user=user))
                       for user in ("owner-a", "owner-b", "owner-c")]
            await asyncio.sleep(0)
            assert channel.queue.qsize() == 1
            pending = channel.next_request()
            assert pending is not None and pending.request == Request(op="replan")
            pending.future.set_result(Reply(text="replanned"))
            replies = await asyncio.gather(*callers)
    assert [reply.text for reply in replies] == ["replanned"] * 3
    assert caplog.text.count("Replan requested") == 3
    for user in ("owner-a", "owner-b", "owner-c"):
        assert f"requester={user}" in caplog.text


async def test_timed_out_replan_caller_neither_cancels_work_nor_other_replies(
    tmp_path: Path,
) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                channel.submit("replan", ingress_user="owner-a"), timeout=0.01
            )
        later = asyncio.create_task(channel.submit("replan", ingress_user="owner-b"))
        await asyncio.sleep(0)
        # The still-queued replan was not cancelled by the first caller's timeout.
        assert channel.queue.qsize() == 1
        pending = channel.next_request()
        assert pending is not None
        pending.future.set_result(Reply(text="shared"))
        assert (await later).text == "shared"


async def test_replan_shutdown_resolves_every_waiter(tmp_path: Path) -> None:
    channel = DaemonChannel(settings_at(tmp_path))
    async with channel:
        waiters = [asyncio.create_task(channel.submit("replan", ingress_user=f"user-{i}"))
                   for i in range(3)]
        await asyncio.sleep(0)
        assert channel.queue.qsize() == 1
    replies = await asyncio.gather(*waiters)
    assert [reply.error for reply in replies] == ["daemon stopped"] * 3


async def test_replan_after_work_started_creates_new_work(tmp_path: Path) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        first = asyncio.create_task(channel.submit("replan", ingress_user="owner-a"))
        await asyncio.sleep(0)
        taken = channel.next_request()
        assert taken is not None
        later = asyncio.create_task(channel.submit("replan", ingress_user="owner-b"))
        await asyncio.sleep(0)
        assert channel.queue.qsize() == 1
        fresh = channel.next_request()
        assert fresh is not None and fresh is not taken
        taken.future.set_result(Reply(text="first"))
        fresh.future.set_result(Reply(text="second"))
        assert (await first).text == "first"
        assert (await later).text == "second"


async def test_mode_request_keeps_priority_over_combined_replans(tmp_path: Path) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        replans = [asyncio.create_task(channel.submit("replan", ingress_user=f"user-{i}"))
                   for i in range(3)]
        mode = asyncio.create_task(channel.submit_mode("observe", 4))
        await asyncio.sleep(0)
        assert channel.queue.qsize() == 1
        first = channel.next_request()
        assert first is not None and first.request.op == "mode"
        combined = channel.next_request()
        assert combined is not None and combined.request.op == "replan"
        first.future.set_result(Reply())
        combined.future.set_result(Reply())
        await asyncio.gather(mode, *replans)


async def test_private_socket_and_exclusive_daemon(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    async with DaemonChannel(settings) as channel:
        assert stat.S_IMODE(channel.path.stat().st_mode) == 0o600
        with pytest.raises(RuntimeError, match="already running"):
            async with DaemonChannel(settings):
                pytest.fail("second daemon started")
        task = asyncio.create_task(submit_socket(settings, "apply"))
        pending = await asyncio.wait_for(channel.queue.get(), 1)
        assert pending.request == Request(op="apply")
        pending.future.set_result(Reply(text="daemon plan"))
        assert (await task).text == "daemon plan"
    assert not channel.path.exists()


@pytest.mark.parametrize("payload", [b'{"op":"mode"}\n', b'{"op":"apply","identity":"cli"}\n',
                                     b'not json\n', b'x' * 4097 + b'\n'])
async def test_bad_requests_never_enqueued(tmp_path: Path, payload: bytes) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        reader, writer = await asyncio.open_unix_connection(str(channel.path))
        writer.write(payload)
        await writer.drain()
        assert b'error' in await asyncio.wait_for(reader.readline(), 1)
        assert channel.queue.empty()
        writer.close()
        await writer.wait_closed()


async def test_stale_socket_replaced_and_shutdown_drains_queue(tmp_path: Path) -> None:
    settings = settings_at(tmp_path)
    channel = DaemonChannel(settings)
    channel.path.write_text("stale")
    async with channel:
        task = asyncio.create_task(channel.submit("replan"))
        await asyncio.sleep(0)
        assert not channel.queue.empty()
    assert (await task).error == "daemon stopped"
    assert (await channel.submit("apply")).error == "daemon stopped"
    async with DaemonChannel(settings) as restarted:
        assert restarted.path.is_socket()


async def test_request_read_timeout_rejects_without_execution(tmp_path: Path) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        reader, writer = await asyncio.open_unix_connection(str(channel.path))
        writer.write(b'{"op":')
        await writer.drain()
        assert b'invalid daemon request' in await asyncio.wait_for(reader.readline(), 6)
        assert channel.queue.empty()
        writer.close()
        await writer.wait_closed()


def test_peer_uid_is_kernel_reported_uid() -> None:
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        writer = SimpleNamespace(get_extra_info=lambda name: left)
        assert daemon_channel._peer_uid(writer) == os.getuid()  # type: ignore[arg-type]
    assert daemon_channel._peer_uid(SimpleNamespace(get_extra_info=lambda name: None)) is None  # type: ignore[arg-type]


async def test_reset_on_close_still_returns_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reset(self: asyncio.StreamWriter) -> None:
        raise ConnectionResetError

    settings = settings_at(tmp_path)
    async with DaemonChannel(settings):
        monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", reset)
        reply = await submit_socket(settings, "authority_status")
    assert reply is not None and reply.authority is not None


@pytest.mark.parametrize("uid", [None, os.getuid() + 1])
async def test_foreign_or_unknown_peer_refused_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uid: int | None
) -> None:
    monkeypatch.setattr(daemon_channel, "_peer_uid", lambda writer: uid)
    settings = settings_at(tmp_path)
    async with DaemonChannel(settings) as channel:
        reply = await submit_socket(settings, "apply")
        assert reply is not None and reply.error == "daemon request refused"
        assert channel.queue.empty()


async def test_owner_mode_request_is_served_before_queued_work(tmp_path: Path) -> None:
    async with DaemonChannel(settings_at(tmp_path)) as channel:
        work = [asyncio.create_task(channel.submit("apply")) for _ in range(3)]
        mode = asyncio.create_task(channel.submit_mode("observe", 4))
        await asyncio.sleep(0)
        first = channel.next_request()
        assert first is not None and first.request.op == "mode"
        assert channel.next_request().request.op == "apply"  # type: ignore[union-attr]
        for task in (*work, mode):
            task.cancel()
