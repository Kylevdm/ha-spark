"""The dated charge device intent: built for native Solis inverters only (#699)."""

from dataclasses import replace
from datetime import time, timedelta

import pytest

from ha_spark.api.native_apply import (
    DatedChargeNone,
    DatedChargeUntrusted,
    DatedChargeWindow,
    build_intents,
)
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext
from ha_spark.devices.outcomes import ActionOutcome
from ha_spark.energy import models
from ha_spark.energy.models import ChargeIntent
from ha_spark.energy.soc_integrity import SocMeasurement, SocStatus
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import LoadedTopology
from tests.native_helpers import NOW

SETTINGS = Settings(battery_capacity_kwh=10, battery_voltage_v=50, charge_efficiency=1)
START = NOW.replace(hour=12, minute=0, second=0, microsecond=0)


def _topology(*inverters: tuple[str, str, float]) -> Topology:
    return Topology.model_validate({
        "connections": [{"id": "connection", "supply_voltage_v": 230.0,
                         "import_limit_a": 100.0, "export_limit_a": 100.0,
                         "dno_export_limit_kw": 3.68}],
        "banks": [{"id": "bank", "connection_id": "connection",
                   "usable_capacity_kwh": 10, "nominal_voltage_v": 50,
                   "min_soc": 10.0, "target_cap": 100.0,
                   "charge_limit_a": 62.5, "discharge_limit_a": 62.5,
                   "served_load_share_pct": 100.0}],
        "inverters": [{"id": inverter_id, "bank_id": "bank", "driver_kind": "solis",
                       "charge_limit_a": limit_a, "discharge_limit_a": limit_a,
                       "confirmed": True, "transport": transport,
                       **({"gateway_host": f"{inverter_id}.test"} if transport == "native"
                          else {})}
                      for inverter_id, transport, limit_a in inverters],
    })


ONE = _topology(("inverter", "native", 62.5))
TWO = _topology(("first", "native", 40.0), ("second", "native", 40.0))


def _build(intent: ChargeIntent, topology: Topology = ONE,
           api_version: int = 5) -> list[dict[str, object]]:
    context = ExecutionContext()
    context.limits_record = LoadedTopology("native", 1, topology)
    return build_intents(intent, SETTINGS, context, topology, api_version=api_version)


def _intent(*windows: models.DatedChargeWindow, ok: bool = True,
            trusted: bool = True) -> ChargeIntent:
    soc = (SocMeasurement(SocStatus.OK, NOW, value=40) if ok
           else SocMeasurement(SocStatus.UNAVAILABLE, NOW))
    return ChargeIntent(60, soc, time(23, 30), time(5, 30), daytime_windows=windows,
                        daytime_trusted=trusted)


def _window(start_h: float, end_h: float, energy_kwh: float) -> models.DatedChargeWindow:
    return models.DatedChargeWindow(START + timedelta(hours=start_h),
                                    START + timedelta(hours=end_h), 80.0, energy_kwh)


def _dated(raw: list[dict[str, object]]) -> list[dict[str, object]]:
    return [intent for intent in raw if intent["kind"] == "dated_charge"]


def test_purchase_yields_a_window_sized_to_its_energy():
    # 2 kWh stored over 1 h at 50 V and 100% efficiency is 40 A.
    [dated] = _dated(_build(_intent(_window(0, 1, 2.0))))
    assert dated["request"] == "window"
    assert dated["current_a"] == 40.0
    window = DatedChargeWindow.model_validate(dated)
    assert (window.start, window.end) == (START, START + timedelta(hours=1))


def test_efficiency_raises_the_grid_current():
    settings = SETTINGS.model_copy(update={"charge_efficiency": 0.8})
    context = ExecutionContext()
    context.limits_record = LoadedTopology("native", 1, ONE)
    [dated] = _dated(build_intents(_intent(_window(0, 1, 2.0)), settings, context, ONE,
                                   api_version=5))
    assert dated["current_a"] == 50.0


@pytest.mark.parametrize("energy_kwh", [2.0, 50.0])
def test_two_inverters_share_the_bank_current_within_its_limit(energy_kwh):
    dated = _dated(_build(_intent(_window(0, 1, energy_kwh)), TWO))
    assert [d["inverter_id"] for d in dated] == ["first", "second"]
    assert {d["start"] for d in dated} == {START.isoformat().replace("+00:00", "Z")}
    currents = [float(d["current_a"]) for d in dated]  # type: ignore[arg-type]
    assert currents[0] == currents[1] > 0
    assert sum(currents) <= 62.5
    if energy_kwh == 2.0:
        assert sum(currents) == 40.0


def test_touching_windows_merge_and_a_later_one_waits():
    intent = _intent(_window(3, 4, 1.0), _window(1, 2, 2.0), _window(0, 1, 2.0))
    [dated] = _dated(_build(intent))
    window = DatedChargeWindow.model_validate(dated)
    assert (window.start, window.end) == (START, START + timedelta(hours=2))
    assert window.current_a == 40.0  # 4 kWh over 2 h


def test_no_purchase_sends_none_and_unreadable_evidence_sends_untrusted():
    assert _dated(_build(_intent())) == [
        DatedChargeNone(inverter_id="inverter").model_dump(mode="json")]
    assert _dated(_build(_intent(trusted=False))) == [
        DatedChargeUntrusted(inverter_id="inverter").model_dump(mode="json")]
    # An admitted purchase is still sent while another source is unreadable.
    [dated] = _dated(_build(_intent(_window(0, 1, 2.0), trusted=False)))
    assert dated["request"] == "window"


def test_failed_soc_yields_no_intents():
    assert _build(_intent(_window(0, 1, 2.0), ok=False)) == []


def test_legacy_inverters_get_no_dated_charge():
    mixed = _topology(("legacy", "legacy", 40.0), ("native", "native", 40.0))
    built = _build(_intent(_window(0, 1, 2.0)), mixed)
    assert {intent["inverter_id"] for intent in built} == {"native"}
    assert [d["inverter_id"] for d in _dated(built)] == ["native"]


@pytest.mark.parametrize("overnight_h,daytime", [(8, True), (0.5, False), (None, False)])
def test_the_charge_current_follows_the_window_that_fires_next(overnight_h, daytime):
    # One Solis register (43141) holds the current for both slots (#700).
    window_start_at = None if overnight_h is None else START + timedelta(hours=overnight_h)
    intent = replace(_intent(_window(1, 2, 2.0)), window_start_at=window_start_at)
    overnight = _build(_intent())
    built = _build(intent)
    [dated] = _dated(built)
    currents = {raw["kind"]: raw["current_a"] for raw in built if "current_a" in raw}
    expected = 40.0 if daytime else next(
        raw["current_a"] for raw in overnight if raw["kind"] == "charge_current_limit")
    assert currents == {"charge_window": expected, "charge_current_limit": expected,
                        "dated_charge": 40.0}
    assert expected != 40.0 or daytime
    assert dated["current_a"] == 40.0


def test_a_v4_integration_gets_no_dated_charge():
    # A pre-0.40 integration would refuse it as not_available on every tick.
    intent = _intent(_window(0, 1, 2.0))
    assert _build(intent, api_version=4) == [
        raw for raw in _build(intent) if raw["kind"] != "dated_charge"]


@pytest.mark.parametrize("raw", [
    {"start": "2026-11-20T12:00:00", "end": "2026-11-20T13:00:00+00:00"},
    {"start": "2026-11-20T13:00:00Z", "end": "2026-11-20T12:00:00Z"},
    {"start": "2026-11-20T12:00:00Z", "end": "2026-11-20T12:00:00Z"},
])
def test_add_on_schema_rejects_bad_windows(raw):
    with pytest.raises(ValueError):
        DatedChargeWindow.model_validate(
            {"kind": "dated_charge", "inverter_id": "inverter", "request": "window",
             "current_a": 10.0, **raw})


def test_dated_charge_intent_golden():
    import json
    from pathlib import Path

    from ha_spark.api.commands import Command

    body = json.loads((Path(__file__).parent / "fixtures/contract/"
                       "command_apply_dated_charge_intents.json").read_text())
    command = Command.model_validate(body)
    assert command.model_dump(mode="json") == body
    intents = body["payload"]["intents"]
    for model, raw in zip((DatedChargeNone, DatedChargeUntrusted, DatedChargeWindow), intents,
                          strict=True):
        assert model.model_validate(raw).model_dump(mode="json") == raw
    built = _dated(_build(_intent(_window(0, 1, 2.0))))
    assert built == [intents[2] | {"inverter_id": "inverter"}]


async def test_add_on_owned_site_names_the_refusal(monkeypatch):
    from ha_spark.energy.scheduler import run_once
    from tests.solar_source_helpers import entities_source
    from tests.test_scheduler import _AmpsDevice, _patch_run_once_io, _plan_at

    settings = Settings()
    plan = _plan_at(31.0)
    plan = replace(plan, charge_intent=replace(
        plan.charge_intent, daytime_windows=(_window(0, 1, 2.0),)))
    _patch_run_once_io(monkeypatch, plan,
                       _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v))

    result = await run_once(settings, soc=plan.soc, source=entities_source(settings))

    refusals = [line for line in result.action_lines
                if getattr(line, "outcome", None) == ActionOutcome.REFUSED]
    assert [str(line) for line in refusals] == [
        "[REFUSED] daytime purchase: dated charge windows need an integration-owned "
        "Solis site"]
    assert result.applied_plan is plan
