"""Private fixed-operation transport; only the scheduler consumes the work queue.

The owner-only socket authenticates CLI mode requests by kernel peer uid and
0600 permissions. Socket callers cannot claim an ingress identity. Only the daemon
loop persists grants or runs device work; ordinary work requests add no permission.
Config requests call the shared settings service inline and never enter the work queue.
In configuration repair only ``config_show``, ``config_import`` and a read-only
``authority_status`` and ``integration_link`` answer; everything else is refused
with ``configuration_repair``.
Request lines are capped at 256 KiB, including the JSON envelope and newline.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import socket
import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ha_spark.api.contract import IntegrationLink, link_view
from ha_spark.authority import (
    AuthorityStore,
    OperatingMode,
    authority_view,
    load_authority,
    resolve_data_dir,
)
from ha_spark.config import _SECRET_OPTION_KEYS, ConfigError, Settings
from ha_spark.devices.outcomes import ActionLine, ActionOutcome
from ha_spark.settings_service import RepairTokenConflict, RevisionConflict, SettingsService

MAX_CONFIG_BYTES = 256 * 1024
Op = Literal[
    "replan", "apply", "sync_clock", "authority_status", "integration_link", "mode",
    "config_show", "config_export", "config_save", "config_import", "config_set_secret",
]
REFUSED = "[REFUSED] no ha-spark daemon running; hardware writes happen only inside the daemon"
CONFIGURATION_REPAIR = "configuration_repair"


class DaemonAlreadyRunning(RuntimeError):
    """Another executor owns the data directory; startup must stop."""


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Op
    target: OperatingMode | None = None
    expected_epoch: int | None = Field(default=None, ge=1, strict=True)

    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    repair_token: str | None = Field(default=None, max_length=128)
    changes: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    field: str | None = None
    value: str | dict[str, bool] | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> Request:
        if self.op == "mode" and self.target is None:
            raise ValueError("mode requires target")
        if self.op != "mode" and (self.target is not None or self.expected_epoch is not None):
            raise ValueError("mode fields require mode operation")
        allowed = {
            "config_save": {"expected_revision", "changes"},
            "config_import": {"expected_revision", "document"},
            "config_set_secret": {"expected_revision", "field", "value"},
        }.get(self.op, set())
        fields = {"expected_revision", "repair_token", "changes", "document", "field", "value"}
        present = {key for key in fields if getattr(self, key) is not None}
        if self.op == "config_import" and present == {"repair_token", "document"}:
            pass
        elif present != allowed:
            raise ValueError("invalid config operation fields")
        if self.op == "config_set_secret" and self.field not in _SECRET_OPTION_KEYS:
            raise ValueError("invalid secret field")
        if self.op == "config_save" and self.changes is not None:
            if self.changes.keys() & _SECRET_OPTION_KEYS:
                raise ValueError("use config_set_secret for secrets")
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
    integration_link: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    error: str | None = None
    reasons: list[str] = Field(default_factory=list)


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
        self.link_view: Callable[[], dict[str, object]] | None = None
        self.settings_service: SettingsService | None = None
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
                    self._handle, path=self.path, limit=MAX_CONFIG_BYTES
                )
            finally:
                os.umask(mask)
            self.path.chmod(0o600)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        # A startup cancelled before its finally ran must not leave a departed AppState bound.
        self.link_view = None
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

    def _repairing(self) -> bool:
        return self.settings_service is not None and self.settings_service.repair is not None

    def authority_status(self) -> dict[str, Any]:
        if self._repairing():
            assert self.settings_service is not None
            # Repair only reads the store: no initialisation, save or fence.
            loaded = load_authority(self.settings_service.bootstrap)
            view = authority_view(loaded.state, known=loaded.status == "known")
            view["suspended"] = CONFIGURATION_REPAIR
            return view
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

    def request_replan(self) -> None:
        """Best-effort settings notification; ordinary queued work grants no authority."""
        if self._server is None or not self._server.is_serving():
            return
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait(Pending(Request(op="replan"), future))
        except asyncio.QueueFull:
            pass

    async def submit(self, op: Op) -> Reply:
        return await self._submit(Request(op=op))

    async def _submit(self, request: Request, ingress_user: str | None = None) -> Reply:
        if request.op == "authority_status":
            return Reply(authority=self.authority_status())
        if request.op == "integration_link":
            return Reply(integration_link=(self.link_view() if self.link_view else
                         link_view(IntegrationLink(None, None, False), datetime.now(UTC))))
        if self._server is None or not self._server.is_serving():
            return Reply(error="daemon stopped")
        if self._repairing() and request.op not in ("config_show", "config_import"):
            return Reply(error=CONFIGURATION_REPAIR,
                         text="configuration needs repair; import a valid configuration")
        if request.op.startswith("config_"):
            service = self.settings_service
            if service is None:
                return Reply(error="configuration unavailable")
            try:
                if request.op == "config_show":
                    result = service.read_view()
                elif request.op == "config_export":
                    result = service.export()
                elif request.op == "config_import":
                    expected = (request.repair_token if request.repair_token is not None
                                else request.expected_revision)
                    assert request.document is not None and expected is not None
                    result = service.import_document(request.document, expected, "import")
                else:
                    assert request.expected_revision is not None
                    changes = request.changes
                    if request.op == "config_set_secret":
                        assert request.field is not None
                        changes = {request.field: request.value}
                    assert changes is not None
                    result = service.save(changes, request.expected_revision, "cli")
                return Reply(config=result)
            except RevisionConflict as exc:
                return Reply(error="conflict", config={"revision": exc.revision})
            except RepairTokenConflict as exc:
                return Reply(error="conflict", config={"repair_token": exc.token})
            except ConfigError as exc:
                return Reply(error="invalid configuration", reasons=[str(exc)])
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
                if len(raw) > MAX_CONFIG_BYTES or not raw.endswith(b"\n"):
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
    expected_revision: int | None = None, changes: dict[str, Any] | None = None,
    document: dict[str, Any] | None = None, field: str | None = None,
    value: str | dict[str, bool] | None = None, repair_token: str | None = None,
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
        payload = Request(
            op=op, target=target, expected_epoch=expected_epoch,
            expected_revision=expected_revision, changes=changes, document=document,
            field=field, value=value, repair_token=repair_token,
        ).model_dump_json(exclude_none=True).encode() + b"\n"
        if len(payload) > MAX_CONFIG_BYTES:
            return Reply(error="daemon request too large")
        writer.write(payload)
        await writer.drain()
        return Reply.model_validate_json(await reader.readline())
    except (OSError, ValueError):
        return Reply(error="daemon response unavailable; request may have executed")
    finally:
        writer.close()
        await writer.wait_closed()
