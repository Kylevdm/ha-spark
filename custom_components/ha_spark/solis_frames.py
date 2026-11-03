"""Read-only Solis frame table, tolerant decoding and a site-wide polling budget.

Tier A follows docs/research/solis-modbus-parity.md (98ef3cd). Pairs are
high-word first. Polling never includes the identity/serial block. A scheduler
reserves at most one frame per second across all gateways and units; overdue
frames rotate rather than catching up in a burst.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .solis_transport import valid_words


@dataclass(frozen=True)
class Frame:
    bank: str
    address: int
    count: int
    interval: int = 30

    @property
    def key(self) -> str:
        return f"{self.bank}_{self.address}"


FRAMES = (
    Frame("input", 33132, 21, 10), Frame("input", 33022, 6),
    Frame("holding", 43141, 30), Frame("holding", 43007, 1),
    Frame("input", 33161, 29), Frame("input", 33029, 12),
    Frame("input", 33049, 10, 10), Frame("input", 33126, 6, 10),
    Frame("holding", 43073, 2),
)


@dataclass(frozen=True)
class Point:
    frame: Frame
    address: int
    name: str
    count: int = 1
    scale: float = 1
    kind: str = "u16"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    primary: bool = False
    suffix: str = ""

    @property
    def key(self) -> str:
        return f"{self.frame.bank}_{self.address}{self.suffix}"


def _points() -> tuple[Point, ...]:
    points: list[Point] = []

    def add(address: int, name: str, count: int = 1, scale: float = 1,
            kind: str = "u16", unit: str | None = None, primary: bool = False,
            suffix: str = "") -> None:
        frame = next(f for f in FRAMES if f.address <= address < f.address + f.count)
        classes = {"kWh": "energy", "W": "power", "V": "voltage", "A": "current",
                   "%": "battery"}
        points.append(Point(frame, address, name, count, scale, kind, unit,
                            "timestamp" if kind == "clock" else classes.get(unit or ""),
                            "total_increasing" if unit == "kWh" else
                            "measurement" if unit else None, primary, suffix))

    for address, name in ((33029, "PV total energy"), (33031, "PV month energy"),
                          (33033, "PV last month energy"), (33037, "PV year energy"),
                          (33039, "PV last year energy")):
        add(address, name, 2, kind="u32", unit="kWh", primary=address == 33029)
    for address, name in ((33035, "PV today energy"), (33036, "PV yesterday energy")):
        add(address, name, scale=0.1, unit="kWh")
    for index in range(4):
        add(33049 + 2 * index, f"PV{index + 1} voltage", scale=0.1, unit="V")
        add(33050 + 2 * index, f"PV{index + 1} current", scale=0.1, unit="A")
    add(33057, "PV power", 2, kind="s32", unit="W", primary=True)
    add(33126, "Meter total active energy", 2, 0.001, "u32", "kWh")
    add(33128, "Meter voltage", scale=0.1, unit="V")
    add(33129, "Meter current", scale=0.01, unit="A")
    add(33130, "Meter active power", 2, kind="s32", unit="W", primary=True)
    for address, name in ((33161, "Battery charge"), (33165, "Battery discharge"),
                          (33169, "Grid import"), (33173, "Grid export"),
                          (33177, "Consumption")):
        add(address, f"{name} total energy", 2, kind="u32", unit="kWh", primary=True)
        add(address + 2, f"{name} today energy", scale=0.1, unit="kWh")
        add(address + 3, f"{name} yesterday energy", scale=0.1, unit="kWh")
    add(33186, "AC grid port fed-in energy", 2, kind="u32", unit="kWh")
    add(33188, "AC grid port consumption energy", 2, kind="u32", unit="kWh")
    add(33133, "Battery voltage", scale=0.1, unit="V", primary=True)
    add(33134, "Battery current", scale=0.1, kind="s16", unit="A")
    add(33135, "Battery direction", kind="direction", primary=True)
    for address in (33136, 33137, 33138):
        add(address, f"Battery register {address}", scale=0.1)
    add(33139, "Battery SoC", unit="%", primary=True)
    add(33140, "Battery SoH", unit="%")
    add(33141, "BMS voltage", scale=0.01, unit="V")
    add(33142, "BMS current", scale=0.1, kind="s16", unit="A")
    add(33143, "BMS charge current", scale=0.1, unit="A")
    add(33144, "BMS discharge current", scale=0.1, unit="A")
    add(33147, "Household load power", unit="W")
    add(33148, "Backup load power", unit="W")
    add(33149, "Battery power", 2, kind="battery", unit="W", primary=True, suffix="_signed")
    add(33151, "Grid-port power", 2, kind="s32", unit="W", primary=True)
    add(33132, "Grid charge permitted", kind="bit5", primary=True, suffix="_b5")
    add(33022, "Inverter clock", 6, kind="clock", primary=True)
    add(43073, "Export limit enabled", kind="bit4", primary=True, suffix="_b4")
    add(43074, "Export limit", scale=100, unit="W", primary=True)
    # No speculative semantics for the remaining words. Reserved words get no entity.
    covered = {(p.frame.bank, a) for p in points
               for a in range(p.address, p.address + p.count) if not p.suffix}
    covered.update(("input", a) for a in (33149, 33150))
    reserved = {43151, 43152, *range(33181, 33186)}
    for frame in FRAMES:
        for address in range(frame.address, frame.address + frame.count):
            if (frame.bank, address) not in covered and address not in reserved:
                points.append(Point(frame, address, f"Register {address}"))
    return tuple(points)


POINTS = _points()


def decode(point: Point, words: tuple[int, ...], zone: ZoneInfo | None,
           *, frame: bool = False) -> float | str | bool | datetime | None:
    """Bad/short words and invalid dates degrade to unknown, never a loop failure."""
    if not valid_words(words):
        return None
    offset = point.address - point.frame.address if frame else 0
    part = words[offset:offset + point.count]
    if len(part) != point.count:
        return None
    value = part[0]
    if point.kind == "clock":
        if zone is None or part[0] > 99:
            return None
        try:
            return datetime(2000 + part[0], part[1], part[2], part[3], part[4], part[5],
                            tzinfo=zone)
        except (ValueError, OverflowError):
            return None
    if point.kind.startswith("bit"):
        return bool(value & (1 << int(point.kind[3:])))
    if point.kind == "direction":
        return {0: "charge", 1: "discharge"}.get(value)
    if point.count == 2:
        value = (value << 16) | part[1]
    if point.kind == "s16" and value >= 0x8000:
        value -= 0x10000
    if point.kind == "s32" and value >= 0x80000000:
        value -= 0x100000000
    if point.kind == "battery":
        if not frame or len(words) != point.frame.count or words[3] not in (0, 1):
            return None
        value *= -1 if words[3] else 1
    if point.unit == "%" and value > 100:
        return None
    return round(float(value) * point.scale, 6)


@dataclass(frozen=True)
class Reading:
    read_at: datetime
    registers: tuple[int, ...] | None
    epoch: int = 0


class Schedule:
    """One reservation per second, rotating inverters and frames fairly."""

    def __init__(self, inverters: tuple[str, ...]) -> None:
        self.items = [(inverter, frame) for frame in FRAMES for inverter in inverters]
        self.due: dict[tuple[str, Frame], float] = {}
        self.cursor = 0
        self.next_at = float("-inf")

    def next(self, now: float) -> tuple[str, Frame] | None:
        if now < self.next_at:
            return None
        for _ in self.items:
            item = self.items[self.cursor]
            self.cursor = (self.cursor + 1) % len(self.items)
            if now >= self.due.get(item, float("-inf")):
                self.due[item] = now + item[1].interval
                self.next_at = now + 1
                return item
        return None
