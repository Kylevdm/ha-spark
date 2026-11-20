"""V2L input reaches the battery without the inverter seeing it (#735)."""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from ha_spark.config import Settings
from ha_spark.energy.soc_integrity import SocStatus
from ha_spark.energy.soc_monitor import SocMonitor, _v2l_input_w, observe_soc
from ha_spark.ha.models import EntityState
from ha_spark.ha.rest import HomeAssistantRest

NOW = datetime(2026, 11, 19, 16, tzinfo=UTC)
CAPACITY = 26.88


def state(value: float, at: datetime) -> EntityState:
    return EntityState(entity_id="sensor.soc", state=str(value), last_reported=at)


def observe(monitor: SocMonitor, value: float, minute: int, *, power: float | None = -1300,
            v2l: float | None = 1740, charge: bool = True):
    """The 2026-11-19 shape: inverter discharging (charging positive) while V2L charges."""
    at = NOW + timedelta(minutes=minute)
    return monitor.check_observation(
        state(value, at), observed_at=at, max_age=timedelta(minutes=10), power_w=power,
        battery_capacity_kwh=CAPACITY, charge_w=power if charge else None, v2l_power_w=v2l,
    )


def test_v2l_netting_inverter_discharge_keeps_unchanged_soc_trusted() -> None:
    # Net +440 W for an hour is 0.44 kWh, inside the 0.5376 kWh tolerance.
    monitor = SocMonitor()
    for minute in range(0, 61):
        assert observe(monitor, 50, minute).ok, minute


def test_same_flow_without_v2l_is_stuck_as_today() -> None:
    monitor = SocMonitor()
    results = [observe(monitor, 50, minute, v2l=None) for minute in range(0, 40)]
    assert results[-1].status is SocStatus.IMPLAUSIBLE
    assert "stuck" in results[-1].reason


def test_v2l_credit_never_helps_an_observation_without_its_own_reading() -> None:
    monitor = SocMonitor()
    for minute in range(0, 25):
        assert observe(monitor, 50, minute).ok
    # 25 min of 1.3 kW is 0.54 kWh of absolute flow, over the 0.5376 kWh stuck threshold.
    result = observe(monitor, 50, 25, v2l=None)
    assert result.status is SocStatus.IMPLAUSIBLE
    assert "stuck" in result.reason


def test_zero_v2l_or_unknown_battery_sign_keeps_absolute_flow() -> None:
    for kwargs in ({"v2l": 0.0}, {"charge": False}):
        monitor = SocMonitor()
        results = [observe(monitor, 50, minute, **kwargs) for minute in range(0, 40)]
        assert results[-1].status is SocStatus.IMPLAUSIBLE, kwargs


def test_v2l_net_flow_that_should_move_soc_is_still_stuck() -> None:
    # Idle inverter, 1.74 kW V2L into a stuck SoC: net flow is real, so it still fails.
    monitor = SocMonitor()
    results = [observe(monitor, 50, minute, power=0) for minute in range(0, 40)]
    assert results[-1].status is SocStatus.IMPLAUSIBLE


def test_v2l_energy_explains_a_rise_but_not_a_jump() -> None:
    monitor = SocMonitor()
    for minute in range(0, 30):
        observe(monitor, 50, minute, power=0)
    # 30 min of 1.74 kW explains about 3.2 points plus tolerance.
    assert observe(monitor, 53, 30, power=0).ok
    assert observe(monitor, 73, 31, power=0).status is SocStatus.IMPLAUSIBLE


def test_invalid_v2l_values_are_no_evidence() -> None:
    for v2l in (-5.0, float("nan"), float("inf")):
        monitor = SocMonitor()
        results = [observe(monitor, 50, minute, v2l=v2l) for minute in range(0, 40)]
        assert results[-1].status is SocStatus.IMPLAUSIBLE, v2l


def _v2l(state_: str, *, age_s: float = 5, unit: str | None = "W") -> EntityState:
    return EntityState(
        entity_id="sensor.v2l", state=state_,
        attributes={"unit_of_measurement": unit} if unit else {},
        last_reported=NOW - timedelta(seconds=age_s),
    )


def test_v2l_reading_must_be_fresh_explicit_watts_and_non_negative() -> None:
    assert _v2l_input_w(_v2l("1740"), NOW) == 1740
    assert _v2l_input_w(_v2l("0"), NOW) == 0
    for bad in (_v2l("1740", age_s=600), _v2l("1740", age_s=-5), _v2l("1740", unit="kW"),
                _v2l("1740", unit=None), _v2l("unavailable"), _v2l("-3"), _v2l("nan")):
        assert _v2l_input_w(bad, NOW) is None


def _json(entity_id: str, value: str, reported: datetime, unit: str | None = None) -> dict:
    return {"entity_id": entity_id, "state": value, "last_reported": reported.isoformat(),
            "attributes": {"unit_of_measurement": unit} if unit else {}}


@respx.mock
async def test_observe_soc_reads_v2l_and_signs_battery_power() -> None:
    settings = Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc",
        battery_power_entity="sensor.power", battery_power_discharge_sign="positive",
        v2l_power_entity="sensor.v2l",
    )
    monitor = SocMonitor()
    now = datetime.now(UTC)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(200, json=_json("sensor.soc", "50", now)))
    # Positive discharge sign: +1300 W is discharging, i.e. -1300 W charging.
    respx.get("http://ha.test/api/states/sensor.power").mock(
        return_value=httpx.Response(200, json=_json("sensor.power", "1300", now, "W")))
    v2l = respx.get("http://ha.test/api/states/sensor.v2l").mock(
        return_value=httpx.Response(200, json=_json("sensor.v2l", "1740", now, "W")))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert (await observe_soc(settings, rest, monitor=monitor)).ok
        v2l.mock(return_value=httpx.Response(500))
        assert (await observe_soc(settings, rest, monitor=monitor)).ok
    assert monitor._charge_w == -1300
    assert monitor._v2l_w is None
    assert v2l.call_count == 2


@respx.mock
async def test_observe_soc_without_v2l_entity_reads_no_v2l() -> None:
    settings = Settings(  # type: ignore[call-arg]
        ha_url="http://ha.test", ha_token="t", soc_entity="sensor.soc",
        battery_power_entity="sensor.power",
    )
    monitor = SocMonitor()
    now = datetime.now(UTC)
    respx.get("http://ha.test/api/states/sensor.soc").mock(
        return_value=httpx.Response(200, json=_json("sensor.soc", "50", now)))
    respx.get("http://ha.test/api/states/sensor.power").mock(
        return_value=httpx.Response(200, json=_json("sensor.power", "1300", now, "W")))
    async with HomeAssistantRest(settings.ha_rest_url, settings.auth_token) as rest:
        assert (await observe_soc(settings, rest, monitor=monitor)).ok
    assert monitor._charge_w is None  # no discharge sign configured
    assert monitor._v2l_w is None
