"""Tests for the charger abstraction and PROACTIVE_MODE gating."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time

import httpx
import pytest
import respx

import ha_spark.devices.inverters.solis as solis_driver
from ha_spark.config import Settings
from ha_spark.devices import get_device, inverter_device
from ha_spark.devices.base import Capability, ControlAuthority
from ha_spark.devices.inverters.alphaess import AlphaESSDevice
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a
from ha_spark.energy.models import ChargeIntent
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.ha.rest import HomeAssistantRest


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "ha_url": "http://ha.test",
        "ha_token": "t",
        "proactive_mode": "simulate",
        "charge_current_entity": "number.solisac_timed_charge_current",
        "inverter_power_switch_entity": "select.solisac_power_switch",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _solis_device(
    s: Settings, rest: HomeAssistantRest, *, control: str = "ha_spark"
) -> SolisDevice:
    """SolisDevice over the DeviceConfig Settings synthesizes from its own flat
    entity fields, so respx mocks built against s.charge_current_entity etc.
    line up with the entities SolisDevice actually reads/writes."""
    config = s.devices[0]
    if control != "ha_spark":
        config = config.model_copy(update={"control": ControlAuthority(control)})
    return SolisDevice(config, s, rest)


def _alpha_device(
    s: Settings, rest: HomeAssistantRest, *, control: str = "ha_spark"
) -> AlphaESSDevice:
    config = s.devices[0]
    if control != "ha_spark":
        config = config.model_copy(update={"control": ControlAuthority(control)})
    return AlphaESSDevice(config, s, rest)


def _soc(value: float) -> SocMeasurement:
    now = datetime.now(UTC)
    return SocMeasurement(
        status=SocStatus.OK,
        observed_at=now,
        value=value,
        raw_state=str(value),
        reported_at=now,
        age_s=0.0,
        max_age_s=600.0,
    )


def _bad_soc() -> SocMeasurement:
    return SocMeasurement(
        status=SocStatus.UNAVAILABLE,
        observed_at=datetime.now(UTC),
        raw_state="unavailable",
        max_age_s=600.0,
    )


def _intent(
    target_soc: float = 77.0,
    soc: SocMeasurement | None = None,
    holds: tuple[tuple, ...] = (),
) -> ChargeIntent:
    if soc is None:
        soc = _soc(50.0)
    return ChargeIntent(target_soc, soc, time(23, 30), time(5, 30), holds=holds)


def _state(entity_id: str, state: str) -> dict[str, object]:
    return {"entity_id": entity_id, "state": state, "attributes": {}}


@pytest.fixture(autouse=True)
def _skip_read_back_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep bounded read-back retry tests fast without changing production timing."""

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(solis_driver.asyncio, "sleep", no_sleep)


HUB = "solis_control"
_WINDOW_FIELDS = (
    "timed_charge_start_hours",
    "timed_charge_start_minutes",
    "timed_charge_end_hours",
    "timed_charge_end_minutes",
    "timed_discharge_start_hours",
    "timed_discharge_start_minutes",
    "timed_discharge_end_hours",
    "timed_discharge_end_minutes",
)
_SUFFIX = {1: "", 2: "_2", 3: "_3"}


def _sensor(field: str) -> str:
    return f"sensor.{HUB}_{field}"


def _get(entity_id: str, value: str) -> None:
    respx.get(f"http://ha.test/api/states/{entity_id}").mock(
        return_value=httpx.Response(200, json=_state(entity_id, value))
    )


def _get_seq(entity_id: str, values: list[str]) -> None:
    respx.get(f"http://ha.test/api/states/{entity_id}").mock(
        side_effect=[httpx.Response(200, json=_state(entity_id, v)) for v in values]
    )


def _mock_native(
    *,
    slot1: list[int] | None = None,
    slot2: list[int] | None = None,
    slot3: list[int] | None = None,
    current: str = "0.0",
    bitfield: str = "35",
    switch: str = "Off",
) -> None:
    """Static read-back for the overlay sensors (each GET returns a fixed value)."""
    for slot, vals in ((1, slot1), (2, slot2), (3, slot3)):
        vals = vals if vals is not None else [0] * 8
        for field, v in zip(_WINDOW_FIELDS, vals, strict=True):
            _get(_sensor(field + _SUFFIX[slot]), str(v))
    _get(_sensor("timed_charge_current"), current)
    _get(_sensor("work_mode_bitfield"), bitfield)
    _get("select.solisac_power_switch", switch)
    _mock_refresh()


def _mock_refresh() -> None:
    respx.post("http://ha.test/api/services/homeassistant/update_entity").mock(
        return_value=httpx.Response(200, json=[])
    )


def _write_calls(route: respx.Route, address: int) -> list[object]:
    """The values written to `address` via modbus.write_register."""
    out = []
    for call in route.calls:
        body = json.loads(call.request.content)
        if body.get("address") == address:
            out.append(body.get("value"))
    return out


def test_solis_current_matches_legacy_sizing() -> None:
    # capacity 26.88 kWh, eff 0.90, voltage 51 V, 6.0 h window, max 62.5 A.
    # needed = (77-50)/100*26.88 = 7.2576 kWh; buy = 7.2576/0.9 = 8.064 kWh;
    # kwh_per_amp = 6.0*51/1000 = 0.306; amps = 8.064/0.306 = 26.35 A.
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=62.5,
    )
    assert solis_current_a(_intent(), s) == pytest.approx(26.35, abs=0.05)


def test_solis_current_clamps_to_max() -> None:
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=10.0,
    )
    assert solis_current_a(_intent(target_soc=90.0), s) == 10.0


def test_solis_capabilities_include_rate() -> None:
    s = _settings()
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    assert Capability.CHARGE_RATE in _solis_device(s, rest).capabilities


@respx.mock
async def test_solis_observe_authority_never_writes_even_when_on() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest, control="observe").apply(_intent())
    assert posts.call_count == 0  # observe authority suppresses the write despite "on"
    assert any("[OBSERVE]" in line for line in lines)


@respx.mock
async def test_apply_writes_window_block_and_current_natively() -> None:
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()  # 23:30 -> 05:30
    expected_a = round(solis_current_a(intent, s))
    # Current is confirmed before the previously inactive slot is written.
    _mock_native(slot1=[0] * 8, current="0.0")
    _get_seq(_sensor("timed_charge_current"), ["0.0", f"{expected_a}.0"])
    for field, value in zip(_WINDOW_FIELDS, [23, 30, 5, 30, 0, 0, 0, 0], strict=True):
        _get_seq(_sensor(field), ["0", "0", str(value)])
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    # One block write to 43143: charge 23:30-05:30, discharge half zeroed.
    assert _write_calls(write, 43143) == [[23, 30, 5, 30, 0, 0, 0, 0]]
    # Charge current 43141 written as DC amps x10.
    assert _write_calls(write, 43141) == [expected_a * 10]
    assert [json.loads(call.request.content)["address"] for call in write.calls] == [
        43141,
        43143,
    ]
    assert next(line for line in lines if "charge current" in line).startswith("[APPLIED]")


@respx.mock
async def test_apply_skips_writes_when_already_set() -> None:
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()
    expected_a = round(solis_current_a(intent, s))
    # Everything already at target: no writes (register endurance).
    _mock_native(slot1=[23, 30, 5, 30, 0, 0, 0, 0], current=f"{expected_a}.0")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    assert write.call_count == 0
    assert any(line.startswith("[SKIP]") for line in lines)


@respx.mock
async def test_apply_zero_guards_a_stale_nondriven_slot() -> None:
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()
    expected_a = round(solis_current_a(intent, s))
    # Slot 1 already set (no write); slot 2 carries a stale manual window.
    _mock_native(
        slot1=[23, 30, 5, 30, 0, 0, 0, 0],
        slot2=[19, 0, 20, 0, 0, 0, 0, 0],
        current=f"{expected_a}.0",
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        await _solis_device(s, rest).apply(intent)
    assert _write_calls(write, 43153) == [[0] * 8]  # slot 2 zeroed
    assert _write_calls(write, 43163) == []  # slot 3 already zero: untouched


@respx.mock
async def test_apply_blocks_when_grid_charge_not_permitted() -> None:
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    # bitfield 3 == 0b11: bit 5 (grid charge) unset -> refuse the force.
    _mock_native(bitfield="3")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent())
    assert _write_calls(write, 43143) == []
    assert _write_calls(write, 43141) == []
    window_line = next(line for line in lines if "charge window" in line)
    assert window_line.startswith("[BLOCKED]")
    assert "grid charging not permitted" in window_line


@respx.mock
async def test_simulate_makes_no_service_calls() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent())
    assert posts.call_count == 0
    assert any("SIMULATE" in line for line in lines)


@respx.mock
async def test_on_applies_and_verifies_read_back() -> None:
    respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on")
    intent = _intent()
    expected_a = round(solis_current_a(intent, s))
    want = [23, 30, 5, 30, 0, 0, 0, 0]
    _mock_refresh()
    # Each block sensor: initial active-window check and write-if-changed read
    # show 0; the bounded fresh read-back sees the target.
    for field, v in zip(_WINDOW_FIELDS, want, strict=True):
        _get_seq(_sensor(field), ["0", "0", str(v)])
    _get_seq(_sensor("timed_charge_current"), ["0.0", f"{expected_a}.0"])
    _get(_sensor("work_mode_bitfield"), "35")
    for slot in (2, 3):
        for field in _WINDOW_FIELDS:
            _get(_sensor(field + _SUFFIX[slot]), "0")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    window_line = next(line for line in lines if "charge window" in line)
    assert window_line.startswith("[APPLIED]")


@respx.mock
async def test_on_warns_when_read_back_mismatches() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    intent = _intent()
    # Current applies; slot 1 remains stale after its write and exhausts the
    # bounded fresh-read attempts.
    _mock_native(slot1=[0] * 8, current="0.0")
    expected_a = round(solis_current_a(_intent(), s))
    _get_seq(_sensor("timed_charge_current"), ["0.0", f"{expected_a}.0"])
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    window_line = next(line for line in lines if "charge window" in line)
    assert window_line.startswith("[WARNING]")
    assert "read back slot 1" in window_line


@respx.mock
async def test_on_warns_when_read_back_read_fails() -> None:
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    # Work-mode assert reads fine; the slot read-backs fail.
    _get(_sensor("work_mode_bitfield"), "35")
    respx.route(method="GET").mock(return_value=httpx.Response(500))
    s = _settings(proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent())
    current_line = next(line for line in lines if "charge current" in line)
    window_line = next(line for line in lines if "charge window" in line)
    assert current_line.startswith("[FAILED]")
    assert window_line.startswith("[BLOCKED]")


@respx.mock
async def test_on_isolates_action_failures() -> None:
    respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(500)
    )
    s = _settings(proactive_mode="on")
    _mock_native(slot1=[0] * 8, current="0.0")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent())
    current_line = next(line for line in lines if "charge current" in line)
    window_line = next(line for line in lines if "charge window" in line)
    assert current_line.startswith("[FAILED]")
    assert window_line.startswith("[BLOCKED]")


@respx.mock
async def test_apply_does_not_activate_new_window_when_current_write_fails() -> None:
    """A failed current write cannot expose a new slot at the old current."""
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(500)
    )
    s = _settings(proactive_mode="on", max_charge_current_a=10.0)
    _mock_native(slot1=[0] * 8, current="60.0")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent(target_soc=90.0))
    assert _write_calls(write, 43141) == [100]
    assert _write_calls(write, 43143) == []
    current_line = next(line for line in lines if "charge current" in line)
    window_line = next(line for line in lines if "charge window" in line)
    assert current_line.startswith("[FAILED]")
    assert window_line.startswith("[BLOCKED]")
    assert "planned current was not confirmed" in window_line


@respx.mock
async def test_apply_leaves_already_active_window_when_current_verification_fails() -> None:
    """A current verification failure leaves an existing window deactivated."""
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    refresh = respx.post(
        "http://ha.test/api/services/homeassistant/update_entity"
    ).mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on", max_charge_current_a=10.0)
    # Slot 1 is already active at 60 A. It is zeroed and freshly confirmed
    # before the current transition; the current read-back then stays stale.
    _mock_native(slot1=[23, 30, 5, 30, 0, 0, 0, 0], current="60.0")
    for field, value in zip(_WINDOW_FIELDS, [23, 30, 5, 30, 0, 0, 0, 0], strict=True):
        _get_seq(_sensor(field), [str(value), str(value), "0"])
    _get_seq(_sensor("timed_charge_current"), ["60.0"] * 5)
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent(target_soc=90.0))
    assert _write_calls(write, 43143) == [[0] * 8]
    assert _write_calls(write, 43141) == [100]
    assert [json.loads(call.request.content)["address"] for call in write.calls] == [
        43143,
        43141,
    ]
    assert refresh.call_count == 2
    assert next(line for line in lines if "deactivate timed slot 1" in line).startswith(
        "[APPLIED]"
    )
    assert next(line for line in lines if "charge window" in line).startswith("[BLOCKED]")


@respx.mock
async def test_apply_blocks_current_when_active_window_cannot_be_deactivated() -> None:
    """A failed slot-zero verification leaves no current transition to attempt."""
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    refresh = respx.post(
        "http://ha.test/api/services/homeassistant/update_entity"
    ).mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on", max_charge_current_a=10.0)
    _mock_native(slot1=[23, 30, 5, 30, 0, 0, 0, 0], current="60.0")
    for field, value in zip(_WINDOW_FIELDS, [23, 30, 5, 30, 0, 0, 0, 0], strict=True):
        _get_seq(_sensor(field), [str(value)] * 5)
    _get(_sensor("timed_charge_current"), "60.0")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent(target_soc=90.0))
    assert _write_calls(write, 43143) == [[0] * 8]
    assert _write_calls(write, 43141) == []
    assert refresh.call_count == 1
    assert next(line for line in lines if "deactivate timed slot 1" in line).startswith(
        "[WARNING]"
    )
    assert next(line for line in lines if "charge current" in line).startswith("[BLOCKED]")
    assert next(line for line in lines if "charge window" in line).startswith("[BLOCKED]")


@respx.mock
async def test_current_verification_refreshes_and_retries_after_delayed_update() -> None:
    """A delayed overlay sensor update is accepted within the fixed retry bound."""
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    refresh = respx.post(
        "http://ha.test/api/services/homeassistant/update_entity"
    ).mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    _get_seq(_sensor("timed_charge_current"), ["0.0", "0.0", "40.0"])
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await _solis_device(s, rest).set_charge_rate(2040.0)
    assert _write_calls(write, 43141) == [400]
    assert refresh.call_count == 1
    assert line.startswith("[APPLIED]")


@respx.mock
async def test_current_verification_is_bounded_when_overlay_stays_stale() -> None:
    """A stale overlay produces a bounded warning rather than an infinite poll."""
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    refresh = respx.post(
        "http://ha.test/api/services/homeassistant/update_entity"
    ).mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    _get_seq(_sensor("timed_charge_current"), ["0.0", "0.0", "0.0", "0.0"])
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await _solis_device(s, rest).set_charge_rate(2040.0)
    assert _write_calls(write, 43141) == [400]
    assert refresh.call_count == 1
    assert line.startswith("[WARNING]")
    assert "read back 0 A" in line


@respx.mock
async def test_on_blocks_all_writes_when_soc_invalid() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    intent = _intent(soc=_bad_soc())
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    assert posts.call_count == 0
    assert all(
        line.startswith("[BLOCKED]") and "unavailable" in line for line in lines
    )


@respx.mock
@pytest.mark.parametrize(
    ("measurement", "evidence"),
    [
        (
            SocMeasurement(
                status=SocStatus.STALE,
                observed_at=datetime.now(UTC),
                value=55.0,
                raw_state="55.0",
                reported_at=datetime.now(UTC),
                age_s=900.0,
                max_age_s=600.0,
            ),
            "900s",
        ),
        (
            SocMeasurement(
                status=SocStatus.MALFORMED,
                observed_at=datetime.now(UTC),
                raw_state="forty",
                max_age_s=600.0,
            ),
            "forty",
        ),
        (
            SocMeasurement(
                status=SocStatus.READ_FAILED,
                observed_at=datetime.now(UTC),
                max_age_s=600.0,
            ),
            "read from Home Assistant failed",
        ),
    ],
    ids=["stale", "malformed", "read_failed"],
)
async def test_on_blocks_writes_for_every_failed_status(
    measurement: SocMeasurement, evidence: str
) -> None:
    """Any failed integrity status blocks real writes and names its own reason."""
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent(soc=measurement))

    assert posts.call_count == 0
    assert all(line.startswith("[BLOCKED]") for line in lines)
    assert any(evidence in line for line in lines)


@respx.mock
async def test_alphaess_on_blocks_writes_for_a_stale_soc() -> None:
    """AlphaESS refuses new programming on any failed integrity observation."""
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    stale = SocMeasurement(
        status=SocStatus.STALE,
        observed_at=datetime.now(UTC),
        value=55.0,
        raw_state="55.0",
        reported_at=datetime.now(UTC),
        age_s=900.0,
        max_age_s=600.0,
    )
    s = _settings(proactive_mode="on", inverter="alphaess")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent(soc=stale))

    assert posts.call_count == 0
    assert all(line.startswith("[BLOCKED]") for line in lines)
    assert any("900s" in line for line in lines)


@respx.mock
async def test_on_does_not_block_genuine_zero_soc() -> None:
    """A real 0% reading (soc ok) must NOT be blocked -- that's exactly
    the moment a real charge is most needed."""
    s = _settings(proactive_mode="on")
    intent = _intent(soc=_soc(0))
    _mock_native(slot1=[0] * 8, current="0.0")
    expected_a = round(solis_current_a(intent, s))
    _get_seq(_sensor("timed_charge_current"), ["0.0", f"{expected_a}.0"])
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    assert not any(line.startswith("[BLOCKED]") for line in lines)


@respx.mock
async def test_simulate_unaffected_by_invalid_soc() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(proactive_mode="simulate")
    intent = _intent(soc=_bad_soc())
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(intent)
    assert posts.call_count == 0
    assert all(line.startswith("[SIMULATE]") or line.startswith("[SKIP]") for line in lines)


async def test_off_mode_computes_without_calls() -> None:
    s = _settings(proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _solis_device(s, rest).apply(_intent())
    assert all("OFF" in line or "SKIP" in line for line in lines)


def test_planned_rate_w_matches_current_times_voltage() -> None:
    s = _settings(
        battery_capacity_kwh=26.88,
        charge_efficiency=0.90,
        battery_voltage_v=51.0,
        max_charge_current_a=62.5,
    )
    intent = _intent()
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    expected = solis_current_a(intent, s) * s.battery_voltage_v
    assert _solis_device(s, rest).planned_rate_w(intent) == pytest.approx(expected)


@respx.mock
async def test_set_charge_rate_writes_current_natively_and_applies() -> None:
    write = respx.post("http://ha.test/api/services/modbus/write_register").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(proactive_mode="on", battery_voltage_v=51.0)
    # 2040 W / 51 V = 40 A; decide reads 0 -> write, verify reads 40.
    _mock_refresh()
    _get_seq(_sensor("timed_charge_current"), ["0.0", "40.0"])
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        line = await _solis_device(s, rest).set_charge_rate(2040.0)
    assert _write_calls(write, 43141) == [400]  # 40 A x10
    assert line.startswith("[APPLIED]")


@respx.mock
async def test_read_charge_rate_converts_amps_to_watts() -> None:
    s = _settings(battery_voltage_v=51.0)
    _get(_sensor("timed_charge_current"), "30")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        watts = await _solis_device(s, rest).read_charge_rate()
    assert watts == pytest.approx(30 * 51.0)


@respx.mock
async def test_read_charge_rate_raises_on_unreadable_sensor() -> None:
    s = _settings()
    respx.get(f"http://ha.test/api/states/{_sensor('timed_charge_current')}").mock(
        return_value=httpx.Response(500)
    )
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        with pytest.raises(httpx.HTTPStatusError):
            await _solis_device(s, rest).read_charge_rate()


def test_get_device_selects_by_driver() -> None:
    rest = HomeAssistantRest(_settings().ha_rest_url, _settings().auth_token)
    s = _settings()
    assert isinstance(get_device(s.devices[0], s, rest), SolisDevice)
    alpha_s = _settings(inverter="alphaess")
    assert isinstance(get_device(alpha_s.devices[0], alpha_s, rest), AlphaESSDevice)


def test_inverter_device_picks_inverter_type() -> None:
    rest = HomeAssistantRest(_settings().ha_rest_url, _settings().auth_token)
    s = _settings(inverter="solis")  # synthesizes a main_inverter device
    assert isinstance(inverter_device(s, rest), SolisDevice)


def test_alphaess_capabilities_exclude_rate() -> None:
    s = _settings(inverter="alphaess")
    rest = HomeAssistantRest(s.ha_rest_url, s.auth_token)
    assert Capability.CHARGE_RATE not in _alpha_device(s, rest).capabilities


@respx.mock
async def test_alphaess_apply_writes_window_and_stop_soc() -> None:
    # mode "on": one alphaess.setbatterycharge call with the window + stop-SOC.
    route = respx.post("http://ha.test/api/services/alphaess/setbatterycharge").mock(
        return_value=httpx.Response(200, json=[])
    )
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="ABC123")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent(target_soc=80.0))
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["serial"] == "ABC123"
    assert body["enabled"] is True
    assert body["cp1start"] == "23:30"
    assert body["cp1end"] == "05:30"
    assert body["chargeStopSOC"] == 80
    assert "[APPLIED]" in lines[0]


@respx.mock
async def test_alphaess_apply_simulate_makes_no_call() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(inverter="alphaess", proactive_mode="simulate")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent())
    assert posts.call_count == 0
    assert "[SIMULATE]" in lines[0]


async def test_alphaess_apply_off_mode_computes_without_calls() -> None:
    s = _settings(inverter="alphaess", proactive_mode="off")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent())
    assert "[OFF]" in lines[0]


@respx.mock
async def test_alphaess_apply_blocks_when_soc_invalid() -> None:
    posts = respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    s = _settings(inverter="alphaess", proactive_mode="on")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent(soc=_bad_soc()))
    assert posts.call_count == 0
    assert "[BLOCKED]" in lines[0]


@respx.mock
async def test_alphaess_apply_does_not_block_genuine_zero_soc() -> None:
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="SN123")
    respx.route(method="POST").mock(return_value=httpx.Response(200, json=[]))
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent(soc=_soc(0)))
    assert not any(line.startswith("[BLOCKED]") for line in lines)


@respx.mock
async def test_alphaess_apply_isolates_failure() -> None:
    respx.post("http://ha.test/api/services/alphaess/setbatterycharge").mock(
        return_value=httpx.Response(500)
    )
    s = _settings(inverter="alphaess", proactive_mode="on", alphaess_serial="ABC123")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        lines = await _alpha_device(s, rest).apply(_intent())
    assert "[FAILED]" in lines[0]


async def test_alphaess_set_charge_rate_and_read_charge_rate_are_noops() -> None:
    s = _settings(inverter="alphaess")
    async with HomeAssistantRest(s.ha_rest_url, s.auth_token) as rest:
        charger = _alpha_device(s, rest)
        assert "[SKIP]" in await charger.set_charge_rate(1000.0)
        assert await charger.read_charge_rate() == 0.0
        assert charger.planned_rate_w(_intent()) == 0.0
