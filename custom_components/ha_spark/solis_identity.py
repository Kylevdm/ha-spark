"""Exact native Solis allow-list and caching decision; no HA or bus I/O."""

from collections.abc import Sequence
from math import isfinite
from statistics import median


def identity_reason(model: int, inverter_type: int, tou: int) -> str | None:
    """V2 TOU takes precedence over other identity mismatches."""
    if tou == 0xAA55:
        return "v2_tou_firmware"
    if (model, inverter_type, tou) != (0x0026, 0x2031, 0x0000):
        return "unvalidated_identity"
    return None


def caching_on(times: Sequence[float]) -> bool | None:
    """Only five finite wire times establish a result; exactly 5 ms is not caching."""
    if len(times) != 5 or any(not isfinite(t) or t < 0 for t in times):
        return None
    return median(times) < 0.005
