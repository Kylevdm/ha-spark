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
