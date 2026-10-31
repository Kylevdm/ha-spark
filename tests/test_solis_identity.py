"""Exact identity and wire-time caching rules, independent of Home Assistant."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "solis_identity", Path(__file__).parents[1] / "custom_components/ha_spark/solis_identity.py",
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
caching_on = module.caching_on
identity_reason = module.identity_reason


@pytest.mark.parametrize(("words", "reason"), [
    ((0x26, 0x2031, 0), None),
    ((0x27, 0x2031, 0), "unvalidated_identity"),
    ((0x26, 0x2030, 0), "unvalidated_identity"),
    ((0x26, 0x2031, 1), "unvalidated_identity"),
    ((0x27, 0x2030, 0xAA55), "v2_tou_firmware"),
])
def test_identity_allowlist(words: tuple[int, int, int], reason: str | None) -> None:
    assert identity_reason(*words) == reason


@pytest.mark.parametrize(("times", "result"), [
    ([.001, .001, .001, .04, .04], True),
    ([.001, .001, .005, .04, .04], False),
    ([.04] * 5, False),
    ([.04] * 4, None),
    ([float("nan")] * 5, None),
    ([-1.] * 5, None),
])
def test_caching_median(times: list[float], result: bool | None) -> None:
    assert caching_on(times) is result
