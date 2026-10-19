"""Device verdicts carried alongside string-compatible presentation lines.

Only explicit, typed read-back verdicts establish hardware verification. The
legacy adapter is reserved for compatibility boundaries, never driver results.
"""

from enum import StrEnum


class ActionOutcome(StrEnum):
    VERIFIED = "verified"
    UNCHANGED_VERIFIED = "unchanged_and_verified"
    SIMULATED = "simulated"
    REFUSED = "refused"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class ActionLine(str):
    """A human action line whose verdict does not depend on its wording."""

    outcome: ActionOutcome

    def __new__(cls, text: str, outcome: ActionOutcome) -> "ActionLine":
        line = super().__new__(cls, text)
        line.outcome = outcome
        return line


def is_verified(line: str) -> bool:
    return isinstance(line, ActionLine) and line.outcome in (
        ActionOutcome.VERIFIED,
        ActionOutcome.UNCHANGED_VERIFIED,
    )


def legacy_action_line(line: str) -> ActionLine:
    """Preserve old prefix semantics only at documented compatibility seams."""
    if isinstance(line, ActionLine):
        return line
    for prefix, outcome in (
        ("[FALLBACK]", ActionOutcome.VERIFIED),
        ("[APPLIED]", ActionOutcome.VERIFIED),
        ("[SKIP]", ActionOutcome.UNCHANGED_VERIFIED),
        ("[SIMULATE]", ActionOutcome.SIMULATED),
        ("[OFF]", ActionOutcome.SIMULATED),
        ("[OBSERVE]", ActionOutcome.SIMULATED),
        ("[FAILED]", ActionOutcome.FAILED),
        ("[BLOCKED]", ActionOutcome.BLOCKED),
    ):
        if line.startswith(prefix):
            return ActionLine(line, outcome)
    return ActionLine(line, ActionOutcome.UNVERIFIED)
