"""Pure per-connection supply guard; positive grid power means import.

Time, household day, measured inverter import and the authority mirror are inputs.
Power is AC-equivalent, as in the daemon guard; the clamp converts watts to bank
amps without an efficiency term. No device I/O or authority store lives here.
"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from math import isfinite


@dataclass(frozen=True)
class GuardReading:
    """Grid import, with the source's report time rather than its change time."""

    import_w: float | None
    reported_at: datetime | None


@dataclass(frozen=True)
class InverterMeasurement:
    """Signed power already normalised to positive inverter import."""

    inverter_id: str
    power_w: float | None
    reported_at: datetime | None


@dataclass(frozen=True)
class AuthorityMirror:
    """Collected authority only; None means the mirror is missing or stale."""

    mode: str
    epoch: int | None
    manual: bool
    save_failed: bool
    transition_target: str | None


def mirror_permits(mirror: AuthorityMirror | None) -> bool:
    """Manual and fallback never widen Observe or an unknown authority."""
    return bool(
        mirror is not None and mirror.mode in {"protect", "optimise"}
        and not mirror.save_failed and mirror.transition_target != "observe"
    )


@dataclass(frozen=True, kw_only=True)
class GuardState:
    """Connection state; a loaded cut is a ceiling, not a request to charge."""

    restore_day: date
    cut_kw: float | None = None
    latched: bool = False
    missing_since: datetime | None = None
    last_good_at: datetime | None = None
    over_count: int = 0
    headroom_since: datetime | None = None
    restores_used: int = 0
    epoch: int | None = None
    dormant_reason: str | None = None
    last_headroom_kw: float | None = None
    last_counted_at: datetime | None = None
    restored: bool = False

    @property
    def limiting(self) -> bool:
        """A retained static ceiling is in force."""
        return self.cut_kw is not None

    @property
    def allowance_kw(self) -> float | None:
        """None means there is no guard cut, not an allocation."""
        return self.cut_kw

    @property
    def restores_left(self) -> int:
        """Cuts are not budgeted."""
        return max(0, 48 - self.restores_used)


@dataclass(frozen=True, kw_only=True)
class GuardInputs:
    """One connection's validated limits and last collected charging request."""

    limit_kw: float
    requested_kw: float
    reserve_kw: float
    reading: GuardReading
    measurements: tuple[InverterMeasurement, ...]
    mirror: AuthorityMirror | None
    grid_configured: bool = True
    controllable: bool = True
    household_day: date | None = None


@dataclass(frozen=True)
class GuardDecision:
    """An optional ceiling write, never a charge-start command."""

    allowance_kw: float | None
    write: bool
    warn_reason: str | None


def _fresh(value: float | None, at: datetime | None, now: datetime) -> bool:
    return (
        value is not None and isfinite(value) and at is not None
        and at.utcoffset() is not None and now.utcoffset() is not None
        and 0 <= (now - at).total_seconds() <= 60
    )


def step(state: GuardState, inputs: GuardInputs, now: datetime) -> tuple[GuardState, GuardDecision]:
    """Advance from reports or a timer; repeated cached reports are not spikes."""
    if now.utcoffset() is None:
        return state, GuardDecision(state.cut_kw, False, "invalid_time")
    day = inputs.household_day or now.date()
    now = now.astimezone(UTC)
    if day > state.restore_day:
        state = replace(state, restore_day=day, restores_used=0)
    reason = None
    if not isfinite(inputs.limit_kw) or inputs.limit_kw <= 0:
        reason = "no_supply_limit"
    elif not inputs.grid_configured:
        reason = "no_grid_source"
    elif not inputs.measurements or not inputs.controllable:
        reason = "no_inverter_measurement"
    if reason:
        state = replace(state, dormant_reason=reason, over_count=0, headroom_since=None)
        return state, GuardDecision(state.cut_kw, False, reason)
    permitted = mirror_permits(inputs.mirror)
    epoch = inputs.mirror.epoch if inputs.mirror else None

    def replay() -> bool:
        # A loaded cut is only re-written in its own epoch; a new cut is current.
        return not state.restored or (epoch is not None and epoch == state.epoch)

    requested = inputs.requested_kw
    if not isfinite(requested) or requested < 0:
        requested = 0
    fresh = _fresh(inputs.reading.import_w, inputs.reading.reported_at, now) and all(
        _fresh(m.power_w, m.reported_at, now) for m in inputs.measurements
    )
    changed = False
    if not fresh:
        state = replace(state, missing_since=state.missing_since or state.last_good_at or now,
                        over_count=0, headroom_since=None, dormant_reason="missing_reading")
        if (state.last_good_at is not None and (now - state.last_good_at).total_seconds() >= 120
                and not state.latched and requested > 0 and state.last_headroom_kw is not None
                and state.last_headroom_kw < inputs.reserve_kw):
            cut = min(requested / 2, state.cut_kw if state.cut_kw is not None else requested)
            state = replace(state, cut_kw=cut, latched=True, epoch=epoch, restored=False)
            changed = True
    else:
        assert inputs.reading.import_w is not None and inputs.reading.reported_at is not None
        reported = min(inputs.reading.reported_at,
                       *(m.reported_at for m in inputs.measurements if m.reported_at is not None))
        import_w = inputs.reading.import_w
        limit_w = inputs.limit_kw * 1000
        other_w = import_w - sum(m.power_w or 0 for m in inputs.measurements)
        available_w = max(0, limit_w - other_w)
        available = available_w / 1000
        new_report = state.last_counted_at is None or inputs.reading.reported_at > (
            state.last_counted_at
        )
        count = state.over_count
        if new_report:
            count = count + 1 if import_w > limit_w + 200 else 0
        if state.last_good_at is not None and reported.timestamp() - (
            state.last_good_at.timestamp()
        ) > 60:
            state = replace(state, headroom_since=None, over_count=0)
            count = 1 if import_w > limit_w + 200 else 0
        state = replace(state, last_good_at=reported, missing_since=None,
                        last_headroom_kw=(limit_w - import_w) / 1000,
                        last_counted_at=inputs.reading.reported_at if new_report
                        else state.last_counted_at, over_count=count,
                        dormant_reason=None)
        if count >= 2 and requested > 0:
            cut = min(requested, available, state.cut_kw if state.cut_kw is not None else requested)
            if state.cut_kw is None or cut < state.cut_kw:
                state = replace(state, cut_kw=cut, epoch=epoch, restored=False)
                changed = True
        if state.cut_kw is not None and requested > 0 and available_w >= requested * 1000 + 200:
            start = state.headroom_since or now
            state = replace(state, headroom_since=start)
            if ((now - start).total_seconds() >= 300 and state.restores_left > 0
                    and permitted and replay()):
                state = replace(state, cut_kw=None, latched=False, headroom_since=None,
                                restores_used=state.restores_used + 1, restored=False, epoch=epoch)
                changed = True
        else:
            state = replace(state, headroom_since=None)
    rewrite = state.restored and permitted and replay() and fresh and requested > 0
    if rewrite:
        state = replace(state, restored=False)
    allowance = min(requested, state.cut_kw) if state.cut_kw is not None else requested
    return state, GuardDecision(allowance if state.cut_kw is not None or changed else None,
                               (changed or rewrite) and permitted and replay() and requested > 0,
                               None if permitted and replay() else "authority_unavailable")
