"""Private fixed-operation transport; only the scheduler consumes the work queue.

The owner-only socket authenticates CLI mode requests by kernel peer uid and
0600 permissions. Socket callers cannot claim an ingress identity. Only the daemon
loop persists grants or runs device work; ordinary work requests add no permission.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ha_spark.authority import AuthorityStore, OperatingMode, authority_view, resolve_data_dir
from ha_spark.config import Settings
from ha_spark.devices.outcomes import ActionLine, ActionOutcome

Op = Literal["replan", "apply", "sync_clock", "authority_status", "mode"]
REFUSED = "[REFUSED] no ha-spark daemon running; hardware writes happen only inside the daemon"


class DaemonAlreadyRunning(RuntimeError):
    """Another executor owns the data directory; startup must stop."""


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Op
    target: OperatingMode | None = None
    expected_epoch: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_mode(self) -> Request:
        if self.op == "mode" and self.target is None:
            raise ValueError("mode requires target")
        if self.op != "mode" and (self.target is not None or self.expected_epoch is not None):
            raise ValueError("mode fields require mode operation")
        return self


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
    authority: dict[str, Any] | None = None
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
    ingress_user: str | None = None


class DaemonChannel:
    def __init__(self, settings: Settings) -> None:
        self.path = resolve_data_dir(settings) / "ha_spark.sock"
        self.authority: AuthorityStore | None = None
        self.queue: asyncio.Queue[Pending] = asyncio.Queue(maxsize=64)
        # Owner mode requests skip queued work so a reply reaches the owner in one tick.
        self.mode_queue: asyncio.Queue[Pending] = asyncio.Queue(maxsize=8)
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
        for queue in (self.mode_queue, self.queue):
            while not queue.empty():
                pending = queue.get_nowait()
                if not pending.future.done():
                    pending.future.set_result(Reply(error="daemon stopped"))
        if self._fd is not None:
            self.path.unlink(missing_ok=True)
            os.close(self._fd)
            self._fd = None

    def authority_status(self) -> dict[str, Any]:
        store = self.authority
        return authority_view(
            store.state if store else None, known=store.known if store else False,
            save_failed=store.save_failed if store else False,
        )

    async def submit_mode(
        self, target: OperatingMode, expected_epoch: int | None, *, ingress_user: str | None = None,
    ) -> Reply:
        return await self._submit(
            Request(op="mode", target=target, expected_epoch=expected_epoch), ingress_user,
        )

    def next_request(self) -> Pending | None:
        """The next request for this tick: an owner mode request before any work."""
        for queue in (self.mode_queue, self.queue):
            if not queue.empty():
                return queue.get_nowait()
        return None

    async def submit(self, op: Op) -> Reply:
        return await self._submit(Request(op=op))

    async def _submit(self, request: Request, ingress_user: str | None = None) -> Reply:
        if request.op == "authority_status":
            return Reply(authority=self.authority_status())
        if self._server is None or not self._server.is_serving():
            return Reply(error="daemon stopped")
        future = asyncio.get_running_loop().create_future()
        try:
            queue = self.mode_queue if request.op == "mode" else self.queue
            queue.put_nowait(Pending(request, future, ingress_user))
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
                reply = await self._submit(request)
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


async def submit_socket(
    settings: Settings, op: Op, *, target: OperatingMode | None = None,
    expected_epoch: int | None = None,
) -> Reply | None:
    """None means unreachable. Never retry an accepted request after a lost reply."""
    path = resolve_data_dir(settings) / "ha_spark.sock"
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=1024 * 1024), timeout=5
        )
    except (OSError, TimeoutError):
        return None
    try:
        writer.write(Request(op=op, target=target, expected_epoch=expected_epoch)
                     .model_dump_json().encode() + b"\n")
        await writer.drain()
        return Reply.model_validate_json(await reader.readline())
    except (OSError, ValueError):
        return Reply(error="daemon response unavailable; request may have executed")
    finally:
        writer.close()
        await writer.wait_closed()
