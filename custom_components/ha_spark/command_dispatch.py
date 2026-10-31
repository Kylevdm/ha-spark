"""Session-local command consumption, with cached refusals and pending results.

No payloads are logged. A consumed sequence is never re-run, including when its
handler fails. Session and epoch checks precede every authority-controller call.
"""
from collections.abc import Awaitable, Callable

from .commands import Command, CommandResult

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
