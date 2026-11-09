"""Session-local command consumption, with cached refusals and pending results.

No payloads are logged. A consumed sequence is never re-run, including when its
handler fails. Session and epoch checks precede every authority-controller call.
CommandDispatch fetches and dispatches commands on the coordinator host.
"""
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .channel import async_fetch_commands, async_post_result
from .commands import (
    Command,
    CommandResult,
    FetchRequest,
    ManualPayload,
    ReprobePayload,
    TransitionPayload,
)
from .transitions import TransitionRequest

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .authority import AuthorityStore
    from .authority_control import AuthorityControl
    from .native_clock import NativeClock
    from .native_write import NativeChargeBoundary

Handler = Callable[[Command], Awaitable[CommandResult]]


class CommandConsumer:
    def __init__(self, session_id: str, epoch: Callable[[], int | None],
                 handler: Handler) -> None:
        self.session_id = session_id
        self.epoch = epoch
        self.handler = handler
        self.last_sequence = 0
        self.results: dict[int, CommandResult] = {}

    async def async_consume(self, command: Command) -> CommandResult:
        base = CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome="refused")
        if command.session_id != self.session_id:
            return base.model_copy(update={"reason": "stale_session"})
        if command.sequence in self.results:
            return self.results[command.sequence]
        if command.sequence <= self.last_sequence:
            return base.model_copy(update={"reason": "replayed"})
        self.last_sequence = command.sequence
        result = base.model_copy(update={"outcome": "pending"})
        self.results[command.sequence] = result
        while len(self.results) > 64:
            del self.results[min(self.results)]
        if command.epoch != self.epoch():
            result = base.model_copy(update={"reason": "stale_authority"})
        else:
            try:
                result = await self.handler(command)
            except Exception:
                result = base.model_copy(update={"outcome": "failed"})
        self.results[command.sequence] = result
        while len(self.results) > 64:
            del self.results[min(self.results)]
        return result


class CommandDispatch:
    """Coordinator command fetching and dispatch with session and queue fences."""

    if TYPE_CHECKING:
        entry: ConfigEntry[Any]
        hass: HomeAssistant
        authority: AuthorityStore
        control: AuthorityControl
        writes: NativeChargeBoundary
        clock: NativeClock
        api_version: int
        connected: bool
        integration_session_id: str
        command_consumer: CommandConsumer | None
        _running: bool
        _ended: bool
        _command_fetching: bool
        _command_queued: bool

        async def _async_supervise_export(self, command: Command) -> CommandResult:
            ...

        async def _async_end_supervision(self, command: Command) -> CommandResult:
            ...

        async def async_reprobe(self, inverter_id: str) -> bool:
            ...

    @callback
    def async_commands_pending(self) -> None:
        """A data-free nudge only; never accepts a command from a service caller."""
        if (not self._running or self._ended or not self.connected or self.api_version < 4
                or not self.control.owned()):
            return
        if self._command_fetching:
            self._command_queued = True
            return
        self._command_fetching = True
        self.entry.async_create_background_task(
            self.hass, self._async_commands(), "ha-spark command fetch",
        )

    async def _async_dispatch_command(self, command: Command) -> CommandResult:
        base = CommandResult(session_id=command.session_id, sequence=command.sequence,
                             outcome="refused", reason="not_available")
        if command.kind == "apply":
            return await self.writes.async_apply(command)
        if not self.control.owned():
            return base.model_copy(update={"reason": "not_owner"})
        if command.kind == "sync_clock":
            result = await self.clock.async_command(command.epoch)
            return result.model_copy(update={"session_id": command.session_id,
                                             "sequence": command.sequence})
        if command.kind == "reprobe":
            # #574's admin seam: identity and caching reads only, no register writes.
            reprobe = ReprobePayload.model_validate(command.payload)
            if not await self.async_reprobe(reprobe.inverter_id):
                return base.model_copy(update={"reason": "native_device_missing"})
            return base.model_copy(update={"outcome": "applied", "reason": None})
        if command.kind == "supervise_export":
            return await self._async_supervise_export(command)
        if command.kind == "end_supervision":
            return await self._async_end_supervision(command)
        if command.kind not in ("transition", "manual"):
            return base
        payload = (TransitionPayload.model_validate(command.payload)
                   if command.kind == "transition"
                   else ManualPayload.model_validate(command.payload))
        if payload.expected_epoch != command.epoch:
            # The locked controller checks only expected_epoch; the two must agree.
            return base.model_copy(update={"reason": "stale_authority"})
        request = TransitionRequest.model_validate(payload.model_dump())
        zone = self.control.household_zone()
        if zone is None and request.target == "manual" and request.duration == "until_tomorrow":
            return base.model_copy(update={"reason": "invalid_household_zone"})
        from .owner_services import _manual_time

        decision = await self.control.async_request(
            request, now=dt_util.utcnow(), zone=zone or ZoneInfo("UTC"),
            at=_manual_time(self.entry),
        )
        if decision.refusal is not None:
            return CommandResult.model_validate({**base.model_dump(), "reason": decision.refusal,
                                                  "reasons": list(decision.reasons)})
        state = decision.state
        if state is None:
            return base.model_copy(update={"reason": "authority_unknown"})
        return base.model_copy(update={"outcome": "applied", "reason": None, "epoch": state.epoch})

    async def _async_admin_user(self, payload: Any) -> str | None:
        """CLI is the local owner; an ingress id must resolve to a non-system admin."""
        if payload.requester_kind == "cli":
            return "cli"
        if payload.user_id is None:
            return None
        user = await self.hass.auth.async_get_user(payload.user_id)
        if user is None or not user.is_admin or user.system_generated:
            return None
        return user.id

    async def _async_commands(self) -> None:
        """At most one HTTP fetch plus one queued fetch, including forged nudges."""
        try:
            while True:
                self._command_queued = False
                data = self.entry.data
                if (not self._running or self._ended or not self.connected
                        or not self.control.owned()):
                    return
                session_id = data["session_id"]
                consumer = self.command_consumer
                if consumer is None or consumer.session_id != session_id:
                    consumer = CommandConsumer(
                        session_id, lambda: (self.authority.state.epoch
                                            if self.authority.state is not None else None),
                        self._async_dispatch_command,
                    )
                    self.command_consumer = consumer
                url = f"http://{data['host']}:{data['port']}/integration/commands"
                session = async_get_clientsession(self.hass)
                reply = await async_fetch_commands(
                    session, url=url + "/fetch", token=data["auth_token"],
                    request=FetchRequest(session_id=session_id,
                                         integration_session_id=self.integration_session_id,
                                         last_result_sequence=consumer.last_sequence),
                )
                if not self._running or self._ended or self.entry.data is not data:
                    return
                if reply is not None:
                    for command in reply.commands:
                        if (not self._running or self._ended or self.entry.data is not data
                                or not self.control.owned()):
                            return
                        if reply.session_id != session_id:
                            result = CommandResult(session_id=command.session_id,
                                                   sequence=command.sequence, outcome="refused",
                                                   reason="stale_session")
                        else:
                            result = await consumer.async_consume(command)
                        if not self._running or self._ended or self.entry.data is not data:
                            return
                        await async_post_result(session, url=url + "/result",
                                                token=data["auth_token"], result=result)
                if not self._command_queued:
                    return
        finally:
            self._command_fetching = False
