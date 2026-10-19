"""Private fixed-operation transport; only the scheduler consumes the work queue.

The socket is not an authority boundary: requests add no write permission and
carry no caller identity. Device work never runs in a connection handler. Besides
the 0600 mode, the kernel-reported peer uid must match the daemon's own uid.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ha_spark.authority import resolve_data_dir
from ha_spark.config import Settings
from ha_spark.devices.outcomes import ActionLine, ActionOutcome

Op = Literal["replan", "apply", "sync_clock"]
REFUSED = "[REFUSED] no ha-spark daemon running; hardware writes happen only inside the daemon"


class DaemonAlreadyRunning(RuntimeError):
    """Another executor owns the data directory; startup must stop."""


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Op


class Line(BaseModel):
    text: str
    outcome: ActionOutcome


def action_lines(lines: Sequence[str]) -> list[Line]:
    return [Line(text=str(line), outcome=(line.outcome if isinstance(line, ActionLine)
                                        else ActionOutcome.UNVERIFIED)) for line in lines]


class Reply(BaseModel):
    text: str = ""
    lines: list[Line] = Field(default_factory=list)
    plan: list[dict[str, object]] = Field(default_factory=list)
    synced: bool = False
    error: str | None = None


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """Kernel-reported uid of the connecting process; None if unavailable."""
    sock = writer.get_extra_info("socket")
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", creds)[1])
    except (AttributeError, OSError, struct.error):
        return None


@dataclass
class Pending:
    request: Request
    future: asyncio.Future[Reply]


class DaemonChannel:
    def __init__(self, settings: Settings) -> None:
        self.path = resolve_data_dir(settings) / "ha_spark.sock"
        self.queue: asyncio.Queue[Pending] = asyncio.Queue(maxsize=64)
        self._fd: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()

    async def __aenter__(self) -> DaemonChannel:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise DaemonAlreadyRunning("ha-spark daemon already running") from None
        self._fd = fd
        try:
            self.path.unlink(missing_ok=True)
            mask = os.umask(0o177)
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle, path=self.path, limit=4096
                )
            finally:
                os.umask(mask)
            self.path.chmod(0o600)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._handlers):
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        while not self.queue.empty():
            pending = self.queue.get_nowait()
            if not pending.future.done():
                pending.future.set_result(Reply(error="daemon stopped"))
        if self._fd is not None:
            self.path.unlink(missing_ok=True)
            os.close(self._fd)
            self._fd = None

    async def submit(self, op: Op) -> Reply:
        if self._server is None or not self._server.is_serving():
            return Reply(error="daemon stopped")
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(Pending(Request(op=op), future))
        except asyncio.QueueFull:
            return Reply(error="daemon request queue full")
        # Disconnecting a caller must not cancel an in-flight device sequence.
        return await asyncio.shield(future)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._handlers.add(task)
        try:
            if _peer_uid(writer) != os.getuid():
                writer.write(Reply(error="daemon request refused").model_dump_json().encode()
                             + b"\n")
                await writer.drain()
                return
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=5)
                if len(raw) > 4096 or not raw.endswith(b"\n"):
                    raise ValueError
                request = Request.model_validate_json(raw)
            except (ValueError, ValidationError, TimeoutError):
                reply = Reply(error="invalid daemon request")
            else:
                reply = await self.submit(request.op)
            writer.write(reply.model_dump_json().encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._handlers.discard(task)


async def submit_socket(settings: Settings, op: Op) -> Reply | None:
    """None means unreachable. Never retry an accepted request after a lost reply."""
    path = resolve_data_dir(settings) / "ha_spark.sock"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=1024 * 1024), timeout=5
        )
    except (OSError, TimeoutError):
        return None
    try:
        writer.write(Request(op=op).model_dump_json().encode() + b"\n")
        await writer.drain()
        return Reply.model_validate_json(await reader.readline())
    except (OSError, ValueError):
        return Reply(error="daemon response unavailable; request may have executed")
    finally:
        writer.close()
        await writer.wait_closed()
