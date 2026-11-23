"""Opt-in handover of a dispatch hold's switch to a purchase window (#769, ADR-0015).

#704 showed a forced Slot 2 charge window keeps the battery charging, not feeding
the car, while the window lasts. With the owner's opt-in, a confirmed dispatch
hold may therefore leave its power switch On while a dated charge window that
was armed and read back stands in for it. The handover lives in memory only: a
restart forgets it and the hold reasserts Off on its next tick. The hold takes
the switch back two minutes before the window ends on the inverter's clock,
once trusted SoC is within 2 % of the target, and as soon as any condition
below fails. Every doubt fails towards the hold.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .native_export import clock_face, clock_window_open
from .topology import Inverter

if TYPE_CHECKING:
    from .coordinator import SparkCoordinator
    from .native_evidence import EvidenceRead

OPTION_KEY = "dispatch_handover"
MARGIN = timedelta(minutes=2)
MAX_CLOCK_ERROR = timedelta(seconds=60)
TARGET_MARGIN_PCT = 2.0


@dataclass(frozen=True)
class Handover:
    """A window the hold's switch was (or may have been) handed to."""
    start: datetime
    end: datetime
    target_soc_pct: float
    authority_epoch: int
    connection_epoch: int
    verified_at: datetime  # Slot 2 read back exactly


def opted_in(options: Any) -> bool:
    return options.get(OPTION_KEY) is True


def conditions(c: "SparkCoordinator", inverter: Inverter, start: datetime, end: datetime,
               target_soc_pct: float, now: datetime, evidence: "EvidenceRead") -> bool:
    """Everything a handover needs apart from its own verified window."""
    state = c.authority.state
    # A dispatch purchase is one half-hour: Slot 2 then repeats exactly that occurrence.
    if (end - start > timedelta(minutes=30)
            or not opted_in(c.entry.options) or (c.counted_api_version or 0) < 6
            or state is None or state.committed_mode != "optimise"
            or state.manual_session is not None or state.fallback.active
            or state.transition is not None
            or not c.writes.positive_current_verified(inverter, state)):
        return False
    if not any(h.device.power_switch == inverter.id and h.cause == "dispatch" and h.owned
               and h.confirmed and h.trusted_end is not None and now < h.trusted_end
               for h in state.holds):
        return False
    if not evidence.dispatch_trusted or not any(
            w.start <= now < w.end for w in evidence.dispatches):
        return False
    # Judge the window on the inverter's own clock. Slot 2 has minute resolution
    # and no UTC offset, so the clock sample must share now's offset too.
    from .native_write import CLOCK_FRAME
    zone = c.control.household_zone()
    sample = c.native.reading(inverter, CLOCK_FRAME)
    error = c.writes._clock_error(inverter, now)
    if zone is None or sample is None or error is None or abs(error) > MAX_CLOCK_ERROR:
        return False
    local_now, local_start, local_end = (t.astimezone(zone) for t in (now, start, end))
    if not (local_start.utcoffset() == local_now.utcoffset() == local_end.utcoffset()
            == sample.read_at.astimezone(zone).utcoffset()):
        return False
    inverter_now = local_now.replace(tzinfo=None) + error
    start_face = local_start.replace(tzinfo=None, second=0, microsecond=0)
    end_face = local_end.replace(tzinfo=None, second=0, microsecond=0)
    if (not start_face <= inverter_now < end_face - MARGIN
            or not clock_window_open(inverter_now.time(), start_face.time(), end_face.time())):
        return False
    soc = getattr(c, "soc", None)
    bank = soc.bank(inverter.bank_id) if soc is not None else None
    value = getattr(bank, "value", None)
    return (bank is not None and bank.trust == "trusted" and value is not None
            and value < target_soc_pct - TARGET_MARGIN_PCT)


def live(c: "SparkCoordinator", inverter: Inverter, handover: Handover, now: datetime,
         evidence: "EvidenceRead") -> bool:
    """The handed-over window still stands in for the hold, on every check."""
    from .native_write import CHARGE_BLOCK_FRAME, GRID_CHARGE_PERMITTED_REG
    state = c.authority.state
    zone = c.control.household_zone()
    if (state is None or zone is None or state.epoch != handover.authority_epoch
            or c.native.transport(inverter).connection_epoch != handover.connection_epoch):
        return False
    face = tuple(clock_face(handover.start, handover.end, zone))
    record = next((r for r in state.dated_charges if r.inverter_id == inverter.id), None)
    verified = c.writes.verified_slot2.get(inverter.id)
    if (record is None or record.state != "armed" or record.epoch != state.epoch
            or record.store_id != state.store_id
            or (record.start, record.end) != (handover.start, handover.end)
            or verified is None or verified[0] != state.epoch or verified[1][:4] != face):
        return False
    # A later poll showing zero current, another face or grid charging not permitted
    # ends the proof.
    poll = c.native.reading(inverter, CHARGE_BLOCK_FRAME)
    if (poll is not None and poll.registers is not None and poll.read_at > handover.verified_at
            and (len(poll.registers) < 16 or poll.registers[0] == 0
                 or tuple(poll.registers[12:16]) != face)):
        return False
    battery = c.native.reading(inverter, f"input_{GRID_CHARGE_PERMITTED_REG}")
    if (battery is not None and battery.registers and battery.read_at > handover.verified_at
            and not battery.registers[0] & 32):
        return False
    return conditions(c, inverter, handover.start, handover.end, handover.target_soc_pct,
                      now, evidence)
