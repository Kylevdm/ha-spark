"""Pure horizon geometry: where the plan's slots fall on the clock (#395).

Slot 0 of the planning horizon is the *current* half-hour, not the start of
the next charge window (#394). The horizon runs from now to the start of the
charge window that *follows* the running-or-next one, so the stretch before
tonight's window is real slots and a same-day event before the window falls
inside the horizon.

All arithmetic is on absolute instants: each slot is 30 *real* minutes, so a
daylight-saving night has the right number of slots, and window coverage is
real interval overlap. Window membership is half-open, ``[start, end)``,
everywhere; equal start and end is a 24 h window. A boundary that does not
exist on a DST day (the spring-forward gap) resolves forward, and an ambiguous
one (the fall-back repeat) takes its first occurrence — both are PEP 495
``fold=0`` semantics. ``tariff._in_overnight_window`` keeps its own legacy
(end-inclusive) clock-membership rule for non-geometry callers.

:func:`legacy_geometry` reproduces today's 48-slot layout for hand-built
inputs (``horizon_start`` at a window start, no geometry): the window covers
the first ``int(window_hours * 2)`` slots. ``PlannerInputs.geometry_for``
derives it from the config so legacy callers keep today's exact indexing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ha_spark.energy.models import SLOTS_PER_DAY, PlannerConfig

# One planning slot: 30 real minutes, whatever the wall clock does that night.
SLOT = timedelta(minutes=30)


@dataclass(frozen=True)
class HorizonGeometry:
    """Where the plan's half-hour slots fall on the clock (pure, no I/O)."""

    # Start of the current half-hour: slot 0 of the horizon.
    horizon_start: datetime
    # Start of the charge window that follows the running-or-next one: the
    # horizon ends there (the following window opens at that instant).
    horizon_end: datetime
    # The running-or-next charge window, as absolute half-open bounds.
    window_start: datetime
    window_end: datetime
    # Absolute start of every slot, 30 real minutes apart (UTC arithmetic).
    slot_starts: tuple[datetime, ...]
    # Fraction of each slot covered by the charge window (real interval
    # overlap, so a boundary off the half-hour gives partial coverage). For a
    # part-elapsed slot 0 it is the fraction of the part still to come.
    window_fracs: tuple[float, ...]
    # Half-open ``[first, end)`` index range spanned by the running-or-next
    # window (every slot with any window coverage).
    window_slot_range: tuple[int, int]
    # Fraction of slot 0 still to come (1.0 when ``now`` is on the boundary).
    slot0_remaining_frac: float
    # Local date on which the running-or-next window ends: the day the charge
    # serves (its load forecast is the plan's day).
    served_day: date


def _at(day: date, t: time, tz: ZoneInfo) -> datetime:
    """Wall time ``t`` on ``day`` in ``tz``, as an absolute UTC instant.

    ``fold=0`` (PEP 495): an ambiguous time takes its first occurrence, and a
    nonexistent one (the spring-forward gap) resolves forward, past the gap.
    The instant is converted to UTC because arithmetic on two datetimes that
    share a tzinfo is wall-clock in CPython, which drifts across a transition.
    """
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


def _window_bounds(
    day: date, window_start: time, window_end: time, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """The charge-window instance that starts on ``day``, half-open.

    An equal start and end is a 24 h window (next day's same time); a
    midnight-wrapping window ends the next day. A DST-shrunk night can leave
    ``end`` at or before ``start``; the window then covers nothing rather than
    wrap the clock a second time.
    """
    start = _at(day, window_start, tz)
    end_day = day if window_start < window_end else day + timedelta(days=1)
    return start, _at(end_day, window_end, tz)


def compute_horizon_geometry(
    now: datetime, window_start: time, window_end: time, tz: ZoneInfo
) -> HorizonGeometry:
    """The horizon geometry for a replan at ``now`` (pure, no I/O).

    ``now`` is the household clock (a naive value is read as ``tz``). The
    horizon starts at the current half-hour and ends where the charge window
    that follows the running-or-next one opens.
    """
    if now.tzinfo is None:  # a naive replan time is read on the household clock
        now = now.replace(tzinfo=tz)
    now_utc = now.astimezone(UTC)
    now_local = now_utc.astimezone(tz)
    horizon_start = now_local.replace(
        minute=now_local.minute // 30 * 30, second=0, microsecond=0
    ).astimezone(UTC)

    # The running-or-next window starts within a day of now, one way or the
    # other; tomorrow's window always starts after now, so one is always found.
    today = now_local.date()
    window: tuple[datetime, datetime] | None = None
    upcoming: tuple[datetime, datetime] | None = None
    for offset in (-1, 0, 1):
        bounds = _window_bounds(today + timedelta(days=offset), window_start, window_end, tz)
        if bounds[0] <= now_utc < bounds[1]:
            window = bounds
            break
        if bounds[0] > now_utc and (upcoming is None or bounds[0] < upcoming[0]):
            upcoming = bounds
    if window is None:
        window = upcoming
    if window is None:  # pragma: no cover - tomorrow's start always qualifies
        raise ValueError(f"no charge window after {now_local:%Y-%m-%d %H:%M %Z}")
    window_start_dt, window_end_dt = window

    # The horizon ends where the *following* window opens: the first window
    # start at or after this window's end (equal, for a 24 h window).
    end_day = window_end_dt.astimezone(tz).date()
    horizon_end: datetime | None = None
    for offset in range(3):
        candidate = _at(end_day + timedelta(days=offset), window_start, tz)
        if candidate >= window_end_dt:
            horizon_end = candidate
            break
    if horizon_end is None:  # pragma: no cover - the next day always qualifies
        raise ValueError(f"no charge window after {window_end_dt:%Y-%m-%d %H:%M %Z}")

    # Slots run from the current half-hour in 30-real-minute steps; the last
    # slot may run past the horizon end only by a partial boundary.
    n_slots = math.ceil((horizon_end - horizon_start).total_seconds() / 1800.0)
    slot_starts = tuple(horizon_start + SLOT * i for i in range(n_slots))

    # Slot 0's coverage is over the part still to come, the part its energy
    # totals carry (#394 review).
    fracs: list[float] = []
    for i, slot_start in enumerate(slot_starts):
        lo = max(slot_start, now_utc) if i == 0 else slot_start
        overlap = min(slot_start + SLOT, window_end_dt) - max(lo, window_start_dt)
        fracs.append(max(0.0, overlap / (slot_start + SLOT - lo)))

    covered = [i for i, frac in enumerate(fracs) if frac > 0.0]
    window_slot_range = (covered[0], covered[-1] + 1) if covered else (0, 0)

    remaining = (horizon_start + SLOT - now_utc).total_seconds() / 1800.0

    return HorizonGeometry(
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        window_start=window_start_dt,
        window_end=window_end_dt,
        slot_starts=slot_starts,
        window_fracs=tuple(fracs),
        window_slot_range=window_slot_range,
        slot0_remaining_frac=min(1.0, max(0.0, remaining)),
        served_day=window_end_dt.astimezone(tz).date(),
    )


def legacy_geometry(
    horizon_start: datetime, config: PlannerConfig, n_slots: int = SLOTS_PER_DAY
) -> HorizonGeometry:
    """Today's 48-slot layout for a hand-built ``horizon_start`` (no I/O).

    The window covers the first ``int(window_hours * 2)`` slots and slot 0 is
    whole, exactly as the pre-#394 horizon assumed: ``horizon_start`` at a
    window start, 30-minute slots, no DST awareness. Read it through
    ``PlannerInputs.geometry_for`` rather than calling this directly.
    """
    n_window = int(config.window_hours * 2)
    fracs = tuple(1.0 if i < n_window else 0.0 for i in range(n_slots))
    window_end = horizon_start + timedelta(hours=config.window_hours)
    slot_starts = tuple(horizon_start + SLOT * i for i in range(n_slots))
    return HorizonGeometry(
        horizon_start=horizon_start,
        horizon_end=horizon_start + SLOT * n_slots,
        window_start=horizon_start,
        window_end=window_end,
        slot_starts=slot_starts,
        window_fracs=fracs,
        window_slot_range=(0, min(n_window, n_slots)),
        slot0_remaining_frac=1.0,
        # A legacy input carries no household zone: read the served date on
        # the clock the horizon was built with.
        served_day=window_end.date(),
    )
