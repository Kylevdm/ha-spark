"""Power-switch adapter reserved for #580's verified hold/power helper.

No register I/O is permitted here until that helper is wired by the orchestrator.
"""
from typing import TYPE_CHECKING

from .commands import IntentResult

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator


async def async_set_power_switch(coordinator: "SparkCoordinator", inverter_id: str,
                                 on: bool, *, epoch: int) -> IntentResult:
    """Fail closed until the protection-owned helper is available."""
    return IntentResult(outcome="refused", reason="not_available", written_a=None)
