"""Pure native Solis clock rules, ported from the legacy clock and DST oracle.

Compare wall-clock faces, not UTC instants: resident windows follow the inverter
face. Zone changes are found from zone rules, including after a process restart.
"""
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, tzinfo

CLOCK_SYNC_REG = 43000
STALE_AFTER = timedelta(seconds=60)
DRIFT_TOLERANCE = timedelta(minutes=5)
FAIL_AT = timedelta(minutes=30)
LOOKBACK = timedelta(hours=6)
READ_BACK_TOLERANCE = timedelta(seconds=10)


def clock_error(words: Sequence[int], read_at: datetime, household: tzinfo) -> timedelta | None:
    """Inverter face minus household face at a successful, dated native read."""
    if read_at.tzinfo is None or len(words) != 6:
        return None
    if any(type(word) is not int or not 0 <= word <= 65535 for word in words):
        return None
    yy, mm, dd, hh, mi, ss = words
    if yy > 99:
        return None
    try:
        face = datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None
    return face - read_at.astimezone(household).replace(tzinfo=None)


def clock_registers(now: datetime, household: tzinfo) -> list[int]:
    """Six whole-second household-clock fields for holding register 43000."""
    local = now.astimezone(household)
    return [local.year % 100, local.month, local.day, local.hour, local.minute, local.second]


def last_clock_change(now: datetime, lookback: timedelta = LOOKBACK) -> datetime | None:
    """Find the last zone offset change by whole-second UTC bisection."""
    if now.tzinfo is None:
        return None
    end = int(now.astimezone(UTC).timestamp())
    start = end - int(lookback.total_seconds())

    def offset(second: int) -> timedelta | None:
        return datetime.fromtimestamp(second, UTC).astimezone(now.tzinfo).utcoffset()

    if offset(start) == offset(end):
        return None
    lo, hi = start, end
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if offset(mid) == offset(start):
            lo = mid
        else:
            hi = mid
    return datetime.fromtimestamp(hi, UTC)
