"""Solis inverter clock: read, check against the household clock, and encode a sync (#161).

The Slot 1 window registers hold a clock face with no date or zone, so the
inverter fires them on its *own* clock. The **household clock** (the configured
``timezone``) is authoritative; the **inverter clock** is checked against it and
never trusted on its own (#155).

The clock is read from one overlay entity (``sensor.<hub>_inverter_clock``):
input registers 33022–33027 (yy, mm, dd, hh, mi, ss) read in **one** Modbus
transaction as an HA ``custom`` ``>6H`` sensor. HA renders its state as
``"26,9,28,14,3,0"``. A single transaction cannot tear across midnight, and a
single ``last_updated`` dates the whole reading. The clock advances on every
poll, so ``last_updated`` moves with every read; a frozen clock goes stale.

The sync target is holding register 43000, the same six fields. The year is
``year % 100``, matching solax-modbus ``value_function_sync_rtc_ymd``. There is
no Solis-published source for either register block.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from ha_spark.ha.models import EntityState

CLOCK_SYNC_REG = 43000
# A reading older than this cannot vouch for the clock now (#155 decision 4).
STALE_AFTER = timedelta(seconds=60)
# A missed daylight-saving change, or worse: `health` fails rather than warns.
FAIL_AT = timedelta(minutes=30)
UNREADABLE = "inverter clock unreadable"


def clock_entity(hub: str) -> str:
    """The overlay's clock sensor on the ``solis_control`` hub named ``hub``."""
    return f"sensor.{hub}_inverter_clock"


def clock_error(state: EntityState, household: tzinfo) -> timedelta | None:
    """Inverter clock minus household clock at the moment of the reading.

    Both sides are compared as wall-clock faces, since that is what the Slot 1
    registers fire on: an inverter left on GMT reads an hour behind in BST.
    ``None`` when the reading is unreadable: any field missing, non-integer or
    out of range, or no ``last_updated`` to date it.
    """
    # A naive timestamp cannot be placed on any clock (and would raise below).
    if state.last_updated is None or state.last_updated.tzinfo is None:
        return None
    fields = state.state.split(",")
    if len(fields) != 6:
        return None
    try:
        values = [float(field) for field in fields]
    except ValueError:
        return None
    if not all(value.is_integer() for value in values):
        return None
    yy, mm, dd, hh, mi, ss = (int(value) for value in values)
    if not 0 <= yy <= 99:
        return None
    try:
        face = datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None
    household_face = state.last_updated.astimezone(household).replace(tzinfo=None)
    return face - household_face


def describe_error(error: timedelta) -> str:
    """``"6 min ahead"`` / ``"60 min behind"`` / ``"35 s ahead"``."""
    seconds = abs(error.total_seconds())
    size = f"{seconds / 60:.0f} min" if seconds >= 60 else f"{seconds:.0f} s"
    return f"{size} {'ahead' if error >= timedelta(0) else 'behind'}"


def clock_refusal(
    state: EntityState | None, now: datetime, household: tzinfo, tolerance: timedelta
) -> str | None:
    """Why an export window must not be armed on this clock, or ``None`` to proceed."""
    error = None if state is None else clock_error(state, household)
    if state is None or error is None or state.last_updated is None:
        return UNREADABLE
    age = now - state.last_updated
    if age > STALE_AFTER:
        return f"inverter clock reading is {age.total_seconds():.0f} s old"
    if abs(error) > tolerance:
        return (
            f"inverter clock is {describe_error(error)} of the household clock, outside "
            f"the {tolerance.total_seconds() / 60:g} min tolerance; "
            "run `python -m ha_spark solis sync-clock`"
        )
    return None


def clock_registers(now: datetime, household: tzinfo) -> list[int]:
    """The 43000 block for ``now`` on the household clock: yy, mm, dd, hh, mi, ss."""
    local = now.astimezone(household)
    return [local.year % 100, local.month, local.day, local.hour, local.minute, local.second]
