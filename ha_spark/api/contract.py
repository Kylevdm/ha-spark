"""The add-on's half of the add-on/integration wire contract (#324).

`API_VERSION` is the `api_version` the add-on puts on every plan summary and
accepts on every site snapshot. It is an integer, separate from the release
version, and bumps only for breaking contract changes: additive fields are
ignored by receivers that don't know them.

The integration's matching constant is `custom_components/ha_spark/wire.py`.
Both suites compare their constant with `tests/fixtures/contract/contract.json`,
so neither half can move a contract version alone.
"""

from datetime import datetime
from typing import Final

from ha_spark import __version__

API_VERSION: Final = 1


def plan_summary(*, session_id: str, built_at: datetime) -> dict[str, object]:
    """Project the phase-4 envelope without any daemon or household state."""
    return {
        "api_version": API_VERSION,
        "release_version": __version__,
        "session_id": session_id,
        "built_at": built_at.isoformat(),
        "commands_pending": False,
        "plan": None,
    }
