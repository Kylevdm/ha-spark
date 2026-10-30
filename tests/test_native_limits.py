"""#542: accepted native limits drive planning and the legacy writer on an exact match."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
import respx

from ha_spark.api.topology_intake import accept_topology
from ha_spark.devices.execution import Ownership, execution_context
from ha_spark.devices.inverters.solis import (
    FallbackSchedule,
    active_limits,
    solis_current_a,
    validate_fallback_schedule,
)
from ha_spark.energy import scheduler
from ha_spark.energy.planner import compute_plan
from ha_spark.energy.tariff import fixed_schedule
from ha_spark.energy.topology import Topology, effective_limits
from ha_spark.energy.topology_record import (
    PREVIEW_REASON,
    UNAVAILABLE_REASON,
    UNCONFIRMED_REASON,
    LimitsSelection,
    LoadedTopology,
    load_topology_record,
    save_topology_record,
    select_limits,
)
from ha_spark.ha.rest import HomeAssistantRest
from ha_spark.health import check_limits
from ha_spark.mode_preconditions import check_preconditions
from tests.authority_helpers import legacy_settings as Settings
from tests.authority_helpers import seed_authority
from tests.test_mode_preconditions import ha_response, measurement
from tests.test_mode_preconditions import settings as precondition_settings
from tests.test_planner_limits import config, inputs
from tests.test_scheduler import _AmpsDevice, _patch_run_once_io, _plan_at, entities_source
from tests.test_solis_power_switch import FakeRest, _device

pytestmark = pytest.mark.usefixtures("legacy_solis_hub")


def _connection(cid: str = "grid") -> dict[str, object]:
    return {"id": cid, "supply_voltage_v": 240.0, "import_limit_a": 80.0,
            "export_limit_a": 80.0, "dno_export_limit_kw": 3.68}


def _bank(bid: str = "bank", cid: str = "grid", share: float = 100.0) -> dict[str, object]:
    return {"id": bid, "connection_id": cid, "usable_capacity_kwh": 100.0,
            "nominal_voltage_v": 52.0, "min_soc": 20.0, "target_cap": 90.0,
            "charge_limit_a": 100.0, "discharge_limit_a": 100.0,
            "served_load_share_pct": share}


def _inverter(
    iid: str = "inv", bid: str = "bank", charge: float = 40.0, *, confirmed: bool = True,
    kind: str = "solis",
) -> dict[str, object]:
    return {"id": iid, "bank_id": bid, "driver_kind": kind, "charge_limit_a": charge,
            "discharge_limit_a": 62.5, "confirmed": confirmed}


def single(charge: float = 40.0, *, confirmed: bool = True) -> Topology:
    return Topology.model_validate({
        "connections": [_connection()], "banks": [_bank()],
        "inverters": [_inverter(charge=charge, confirmed=confirmed)],
    })


def multi_bank() -> Topology:
    return Topology.model_validate({
        "connections": [_connection()],
        "banks": [_bank("bank", share=60.0), _bank("annex", share=40.0)],
        "inverters": [_inverter(charge=50.0), _inverter("annex_inv", "annex", 30.0)],
    })


def native(topology: Topology, revision: int = 1, **kw: object) -> LoadedTopology:
    return LoadedTopology("native", revision, topology, **kw)  # type: ignore[arg-type]


@pytest.fixture
def record():
    """Bind an accepted limits record to the process context for one test."""
    context = execution_context()

    def bind(loaded: LoadedTopology | None) -> None:
        context.limits_record = loaded
        context.planned_limits.set(context.limits_key())

    yield bind
    context.limits_record = None
    context.planned_limits.set(("legacy", None))


def test_exact_single_inverter_match_drives_plan_and_driver_at_40_a(record) -> None:
    settings = Settings(battery_capacity_kwh=100, battery_voltage_v=52, charge_efficiency=0.8,
                        max_charge_current_a=62.5, timezone="UTC")
    selection = select_limits(native(single()), settings)
    assert selection == LimitsSelection("native", single())
    cfg, inp = config(), inputs()
    topology = selection.actuation
    assert topology is not None
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), topology=topology,
                        limits=effective_limits(topology))
    assert plan.effective_limits is not None
    assert plan.effective_limits.inverters[0].charge_a == 40
    assert plan.hardware_limit_reason == "Charging limited by hardware: inv."
    record(native(single()))
    assert plan.charge_intent is not None
    assert solis_current_a(plan.charge_intent, settings) == pytest.approx(40)
    # Without the record the same intent is sized at the legacy 62.5 A ceiling.
    record(None)
    assert solis_current_a(plan.charge_intent, settings) > 40


def test_native_fallback_above_the_inverter_limit_is_refused(record) -> None:
    record(native(single()))
    assert isinstance(validate_fallback_schedule(Settings(solis_fallback_current_a=40.0)),
                      FallbackSchedule)
    refused = validate_fallback_schedule(Settings(solis_fallback_current_a=41.0))
    assert refused == (
        "solis_fallback_current_a (41 A) is above the effective charge limit (40 A)"
    )
    record(None)
    assert isinstance(validate_fallback_schedule(Settings(solis_fallback_current_a=41.0)),
                      FallbackSchedule)


def test_unconfirmed_native_limit_narrows_to_zero(record) -> None:
    record(native(single(confirmed=False)))
    result = active_limits(Settings())
    assert not isinstance(result, str)
    assert result[1].inverters[0].charge_a == 0
    assert isinstance(validate_fallback_schedule(Settings(solis_fallback_current_a=10.0)), str)


async def test_superseded_revision_is_refused_at_write_time(tmp_path, record) -> None:
    rest = FakeRest()
    device = _device(rest, tmp_path)
    context = execution_context()
    record(native(single(), revision=1))
    config_ = device._config
    assert context.gate.mode_for(Ownership.CHARGING, config_, device._settings) == "on"
    # A new accepted revision lands after this write was planned.
    context.limits_record = native(single(30.0), revision=2)
    assert context.gate.recheck(Ownership.CHARGING, config_, device._settings) == (
        "refuse:stale limits"
    )
    line = await device.set_charge_rate(1000)
    assert line.startswith("[REFUSED] stale limits")
    assert not rest.calls
    # Planning again under the new revision restores the grant.
    with context.planning():
        assert context.gate.mode_for(Ownership.CHARGING, config_, device._settings) == "on"


def test_multi_bank_is_preview_only_and_actuation_keeps_previous_limits(record) -> None:
    settings = Settings()
    loaded = native(multi_bank(), revision=2,
                    supported_revision=1, supported_topology=single())
    selection = select_limits(loaded, settings)
    assert selection.source == "native (preview only)"
    assert selection.reason == PREVIEW_REASON
    assert selection.actuation == single()
    assert selection.preview is not None
    assert [b.id for b in selection.preview.banks] == ["bank"]
    record(loaded)
    limits = active_limits(settings)
    assert not isinstance(limits, str)
    assert limits[1].inverters[0].charge_a == 40  # not the preview bank's 50 A
    # With no supported topology ever accepted, actuation stays on the legacy settings.
    assert select_limits(native(multi_bank()), settings).actuation is None
    record(native(multi_bank()))
    limits = active_limits(settings)
    assert not isinstance(limits, str) and limits[1].inverters[0].id == "legacy_inverter"


def test_mismatched_driver_kind_is_preview_only() -> None:
    raw = single().model_dump(mode="json")
    raw["inverters"][0]["driver_kind"] = "alphaess"
    selection = select_limits(native(Topology.model_validate(raw)), Settings())
    assert selection.source == "native (preview only)" and selection.actuation is None


def test_intake_keeps_last_supported_topology_durably(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "events.db"))
    block = {"configured": True, "revision": 1,
             "topology": single().model_dump(mode="json")}
    first, changed = accept_topology(settings, LoadedTopology(), block)
    assert changed and first.supported_revision == 1
    block = {"configured": True, "revision": 2,
             "topology": multi_bank().model_dump(mode="json")}
    second, changed = accept_topology(settings, first, block)
    assert changed and second.topology == multi_bank()
    assert second.supported_topology == single() and second.supported_revision == 1
    reloaded = load_topology_record(settings)
    assert reloaded.supported_topology == single() and reloaded.revision == 2
    # A v1/v2 peer (no topology block) after native acceptance keeps the record.
    kept, changed = accept_topology(settings, reloaded, None)
    assert not changed
    assert kept.kind == "native" and kept.topology == multi_bank()
    assert kept.reason == "topology_not_configured"


def test_unavailable_record_fails_closed(tmp_path, record) -> None:
    settings = Settings(db_path=str(tmp_path / "events.db"), solis_fallback_current_a=10.0)
    save_topology_record(settings, 1, single())
    (tmp_path / "ha_spark_topology.json").write_text("{")
    loaded = load_topology_record(settings)
    assert select_limits(loaded, settings) == LimitsSelection(
        "unavailable", reason=UNAVAILABLE_REASON)
    record(loaded)
    assert validate_fallback_schedule(settings) == (
        "the accepted topology record is unavailable")
    cfg, inp = config(), inputs(event=True)
    plan = compute_plan(inp, cfg, fixed_schedule(inp, cfg), limits_unavailable=UNAVAILABLE_REASON)
    assert plan.limits_reason == UNAVAILABLE_REASON
    assert plan.target_soc <= inp.soc_now and plan.charge_intent is not None
    assert plan.charge_intent.export is None
    assert plan.charge_intent is not None
    assert solis_current_a(replace(plan.charge_intent, target_soc_pct=90), settings) == 0


async def test_unavailable_record_holds_normal_programming(monkeypatch, caplog, record) -> None:
    settings = Settings()
    record(LoadedTopology("unavailable", reason="topology_record_unavailable"))
    current = _plan_at(31.0)
    device = _AmpsDevice({31.0: 38.0}, settings.battery_voltage_v)
    _patch_run_once_io(monkeypatch, current, device)
    fake = scheduler.current_plan

    async def fake_with_selection(*args: object, **kw: object) -> object:
        assert kw["limits"] == execution_context().limits_record
        run = await fake(*args, **kw)
        return SimpleNamespace(**vars(run), limits=LimitsSelection("unavailable"))

    monkeypatch.setattr(scheduler, "current_plan", fake_with_selection)
    caplog.set_level("INFO")
    result = await scheduler.run_once(settings, soc=current.soc, previous_plan=None,
                                      source=entities_source(settings))
    assert device.applied == []
    assert result.applied_plan is None
    assert "normal programming held while the accepted topology record is unavailable" in (
        caplog.text)


@pytest.mark.parametrize("loaded", [
    native(single(confirmed=False)),
    native(Topology.model_validate({
        "connections": [_connection()],
        "banks": [_bank("bank", share=60.0), _bank("annex", share=40.0)],
        "inverters": [_inverter(), _inverter("annex_inv", "annex", confirmed=False)],
    })),
])
@respx.mock
async def test_unconfirmed_limit_blocks_entering_protect_and_optimise(
    tmp_path, record, loaded,
) -> None:
    record(loaded)
    respx.get(url__regex=r"http://ha.test/api/states/.*").mock(side_effect=ha_response)
    async with HomeAssistantRest("http://ha.test/api", "secret") as rest:
        report = await check_preconditions(
            precondition_settings(tmp_path), rest, measurement(), soc_programming_held=False,
        )
    assert UNCONFIRMED_REASON in report.protect
    assert UNCONFIRMED_REASON in report.optimise


async def test_health_shows_the_limits_source(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "events.db"))
    assert (await check_limits(settings)).detail.startswith("legacy: ")
    save_topology_record(settings, 1, single())
    assert (await check_limits(settings)).detail.startswith("native: charge 2.08 kW DC")
    save_topology_record(settings, 2, multi_bank(), (1, single()))
    detail = (await check_limits(settings)).detail
    assert detail.startswith("native (preview only): ")
    assert PREVIEW_REASON in detail and "the writer keeps the native limits" in detail


def test_heartbeat_change_is_never_an_authority_change(tmp_path) -> None:
    settings = Settings(db_path=str(tmp_path / "events.db"))
    store = seed_authority(settings, "observe")
    before = store.state
    accept_topology(settings, LoadedTopology(), {
        "configured": True, "revision": 1, "topology": single().model_dump(mode="json")})
    assert store.state == before


def _bank_limited(bank_a: float, voltage_v: float = 52.0) -> Topology:
    """One 40 A Solis inverter on a bank whose own DC charge limit is ``bank_a``."""
    bank = {**_bank(), "charge_limit_a": bank_a, "nominal_voltage_v": voltage_v}
    return Topology.model_validate({
        "connections": [_connection()], "banks": [bank], "inverters": [_inverter()],
    })


def test_normal_current_is_capped_at_the_bank_charge_limit(record) -> None:
    from tests.test_solis_limits import _saturating_intent

    settings = Settings(battery_voltage_v=52.0, max_charge_current_a=62.5)
    record(native(_bank_limited(30.0)))
    assert solis_current_a(_saturating_intent(), settings) == pytest.approx(30)
    record(native(_bank_limited(0.0)))
    assert solis_current_a(_saturating_intent(), settings) == 0


@pytest.mark.parametrize(("bank_a", "written_raw"), [(30.0, 300), (0.0, 0)])
async def test_restore_current_is_capped_at_the_bank_charge_limit(
    tmp_path, record, bank_a: float, written_raw: int,
) -> None:
    from tests.test_solis_limits import FakeRest as ExportRest
    from tests.test_solis_limits import _current_writes
    from tests.test_solis_limits import _device as limits_device

    rest = ExportRest()
    rest.states["sensor.solis_control_timed_charge_current"] = "40"
    device = limits_device(rest, tmp_path, battery_voltage_v=52.0)
    record(native(_bank_limited(bank_a)))
    await device.set_charge_rate(40.0 * 52.0)
    assert _current_writes(rest) == [written_raw]


async def test_native_currents_convert_at_the_bank_nominal_voltage(tmp_path, record) -> None:
    from tests.test_solis_limits import FakeRest as ExportRest
    from tests.test_solis_limits import _current_writes
    from tests.test_solis_limits import _device as limits_device

    rest = ExportRest()
    device = limits_device(rest, tmp_path, battery_voltage_v=52.0)
    record(native(_bank_limited(100.0, voltage_v=100.0)))
    # 1 kW DC at the bank's 100 V is 10 A, not 19 A at the settings' 52 V.
    await device.set_charge_rate(1000.0)
    assert _current_writes(rest) == [100]
    assert await device.read_charge_rate() == pytest.approx(
        float(rest.states["sensor.solis_control_timed_charge_current"]) * 100.0)


def test_wrong_driver_single_inverter_never_replaces_the_supported_topology(
    tmp_path, record,
) -> None:
    """A single AlphaESS inverter can't drive the Solis writer, so it is not 'supported'."""
    settings = Settings(db_path=str(tmp_path / "events.db"), max_charge_current_a=62.5)
    first, _ = accept_topology(settings, LoadedTopology(), {
        "configured": True, "revision": 1, "topology": single(10.0).model_dump(mode="json")})
    alpha = Topology.model_validate({
        "connections": [_connection()], "banks": [_bank()],
        "inverters": [_inverter(kind="alphaess")],
    })
    second, changed = accept_topology(settings, first, {
        "configured": True, "revision": 2, "topology": alpha.model_dump(mode="json")})
    assert changed and second.topology == alpha
    assert second.supported_topology == single(10.0) and second.supported_revision == 1
    assert load_topology_record(settings).supported_topology == single(10.0)
    record(second)
    limits = active_limits(settings)
    assert not isinstance(limits, str)
    assert limits[1].inverters[0].charge_a == 10  # never the legacy 62.5 A
