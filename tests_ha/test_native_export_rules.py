"""Pure supervised-export bounds, sum rule and fixed-command fit."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from custom_components.ha_spark.authority_store import (
    EventIdentity,
    ExportRecord,
    ExportSupervision,
    ExportWindow,
    GatewayClaim,
    StopLatch,
)
from custom_components.ha_spark.native_export import (
    DeviceExportFact,
    bound_refusal,
    clock_face,
    export_not_yet_armed,
    fixed_export_refusal,
    ratings_refusal,
    record_is_live,
)
from custom_components.ha_spark.topology import Topology, effective_limits
from tests_ha.test_topology_flow import STORED_TOPOLOGY

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
EVENT = EventIdentity(direction="export", start=NOW + timedelta(hours=2),
                      end=NOW + timedelta(hours=3))
CLAIM = GatewayClaim(host="gateway.invalid", port=502, unit_id=1)
IDENTITY = (0x0026, 0x2031, 0x0000)


def site(two: bool = False) -> Topology:
    raw = deepcopy(STORED_TOPOLOGY)
    raw["inverters"][0].update(transport="native", gateway_host="gateway.invalid",
                               gateway_port=502, gateway_unit_id=1)
    if two:
        raw["inverters"].append({
            **raw["inverters"][0], "id": "inv2", "gateway_unit_id": 2,
        })
    return Topology.model_validate(raw)


def facts(topology: Topology, *, validated: bool = True,
          identity: tuple[int, ...] = IDENTITY) -> dict[str, DeviceExportFact]:
    return {inverter.id: DeviceExportFact(validated, identity, CLAIM)
            for inverter in topology.inverters}


def test_a_confirmed_bound_within_the_site_limit_is_accepted() -> None:
    topology = site()
    assert ratings_refusal(EVENT, {"inv1": 3000}, topology, effective_limits(topology),
                           facts(topology), now=NOW) is None


def test_every_connection_inverter_needs_a_bound() -> None:
    topology = site(two=True)
    assert ratings_refusal(EVENT, {"inv1": 3000}, topology, effective_limits(topology),
                           facts(topology), now=NOW) == "export_bound_unverified"


def test_an_unvalidated_device_or_missing_identity_is_unverified() -> None:
    topology = site()
    limits = effective_limits(topology)
    assert ratings_refusal(EVENT, {"inv1": 3000}, topology, limits,
                           facts(topology, validated=False), now=NOW) == (
                               "export_bound_unverified")
    assert ratings_refusal(EVENT, {"inv1": 3000}, topology, limits,
                           facts(topology, identity=()), now=NOW) == "export_bound_unverified"


def test_a_single_bound_and_the_sum_must_fit_the_site_limit() -> None:
    topology = site(two=True)
    limits = effective_limits(topology)
    assert ratings_refusal(EVENT, {"inv1": 4000, "inv2": 1000}, topology, limits,
                           facts(topology), now=NOW) == "export_bounds_exceed_site_limit"
    assert ratings_refusal(EVENT, {"inv1": 2000, "inv2": 2000}, topology, limits,
                           facts(topology), now=NOW) == "export_bounds_exceed_site_limit"


def test_a_zero_site_export_limit_refuses_every_supervision() -> None:
    topology = site()
    connection = topology.connections[0].model_copy(update={"dno_export_limit_kw": 0})
    topology = topology.model_copy(update={"connections": (connection,)})
    assert ratings_refusal(EVENT, {"inv1": 1000}, topology, effective_limits(topology),
                           facts(topology), now=NOW) == "export_bounds_exceed_site_limit"


def test_past_or_malformed_ratings_are_refused() -> None:
    topology = site()
    limits = effective_limits(topology)
    past = EventIdentity(direction="export", start=NOW - timedelta(hours=2),
                         end=NOW - timedelta(hours=1))
    assert ratings_refusal(past, {"inv1": 1000}, topology, limits,
                           facts(topology), now=NOW) == "invalid_intent"
    assert ratings_refusal(EVENT, {"inv1": 0}, topology, limits,
                           facts(topology), now=NOW) == "invalid_intent"
    assert ratings_refusal(EVENT, {"unknown": 1000}, topology, limits,
                           facts(topology), now=NOW) == "invalid_intent"


def test_a_reprobe_identity_or_claim_change_invalidates_the_bound() -> None:
    supervision = ExportSupervision(
        inverter_id="inv1", id=uuid4(), event=EVENT, confirming_user_id="cli",
        epoch=3, ends_at=EVENT.end, rating_w=3000, identity=IDENTITY, claim=CLAIM,
    )
    assert bound_refusal(supervision, DeviceExportFact(True, IDENTITY, CLAIM),
                         site_limit_w=3600, connection_sum_w=3000) is None
    assert bound_refusal(supervision, DeviceExportFact(True, (0x27, 0x2031, 0),
                         CLAIM), site_limit_w=3600,
                         connection_sum_w=3000) == "export_bound_unverified"
    changed = CLAIM.model_copy(update={"unit_id": 2})
    assert bound_refusal(supervision, DeviceExportFact(True, IDENTITY, changed),
                         site_limit_w=3600, connection_sum_w=3000) == (
                             "export_bound_unverified")


def test_the_fixed_command_must_fit_each_inverter_and_bank() -> None:
    topology = site(two=True)
    limits = effective_limits(topology)
    assert fixed_export_refusal(["inv1"], topology, limits) is None
    inverter = topology.inverters[0].model_copy(update={"discharge_limit_a": 30})
    topology = topology.model_copy(update={"inverters": (inverter, topology.inverters[1])})
    assert fixed_export_refusal(["inv1"], topology, effective_limits(topology)) == "limits"
    # Two 62.5 A commands exceed a shared bank's 100 A discharge limit.
    topology = site(two=True)
    bank = topology.banks[0].model_copy(update={"discharge_limit_a": 100})
    topology = topology.model_copy(update={"banks": (bank,)})
    assert fixed_export_refusal(["inv1", "inv2"], topology,
                                effective_limits(topology)) == "limits"


def test_a_live_record_needs_its_epoch_store_supervision_end_and_no_latch() -> None:
    supervision = ExportSupervision(
        inverter_id="inv1", id=uuid4(), event=EVENT, confirming_user_id="cli",
        epoch=3, ends_at=EVENT.end, rating_w=3000, identity=IDENTITY, claim=CLAIM,
    )
    store_id = uuid4()
    record = ExportRecord(
        inverter_id="inv1", event=EVENT, window=ExportWindow(start=EVENT.start, end=EVENT.end),
        store_id=store_id, epoch=3, supervision_id=supervision.id,
        state="armed", last_reconcile_at=NOW,
    )
    assert record_is_live(record, now=NOW, epoch=3, store_id=store_id,
                          supervisions=(supervision,), stop_latches=(),
                          fallback_active=False)
    assert not record_is_live(record, now=NOW, epoch=4, store_id=store_id,
                              supervisions=(supervision,), stop_latches=(),
                              fallback_active=False)
    assert not record_is_live(record, now=NOW, epoch=3, store_id=uuid4(),
                              supervisions=(supervision,), stop_latches=(),
                              fallback_active=False)
    assert not record_is_live(record, now=NOW, epoch=3, store_id=store_id,
                              supervisions=(), stop_latches=(), fallback_active=False)
    latch = StopLatch(inverter_id="inv1", event=EVENT, window_end=EVENT.end, epoch=3)
    assert not record_is_live(record, now=NOW, epoch=3, store_id=store_id,
                              supervisions=(supervision,), stop_latches=(latch,),
                              fallback_active=False)
    assert not record_is_live(record, now=NOW, epoch=3, store_id=store_id,
                              supervisions=(supervision,), stop_latches=(),
                              fallback_active=True)


# --- arming (#144), ported from the legacy clock-face tests -------------------

LONDON = ZoneInfo("Europe/London")


def at(year: int, month: int, day: int, hour: int, minute: int = 0, *, fold: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=LONDON, fold=fold)


def test_a_day_early_window_is_deferred_until_the_previous_clock_window_closes() -> None:
    start, end = at(2026, 9, 15, 18, 30), at(2026, 9, 15, 19, 30)
    for hour in range(20):
        assert export_not_yet_armed(start, end, at(2026, 9, 14, hour)), hour
    for hour in range(20, 24):
        assert not export_not_yet_armed(start, end, at(2026, 9, 14, hour)), hour


def test_the_window_arms_once_the_day_before_has_left_its_clock_window() -> None:
    start, end = at(2026, 9, 15, 18, 30), at(2026, 9, 15, 19, 30)
    assert export_not_yet_armed(start, end, at(2026, 9, 14, 18, 29))
    assert export_not_yet_armed(start, end, at(2026, 9, 14, 18, 31))
    assert export_not_yet_armed(start, end, at(2026, 9, 14, 19, 29))
    assert not export_not_yet_armed(start, end, at(2026, 9, 14, 19, 30))
    assert not export_not_yet_armed(start, end, at(2026, 9, 15, 12, 0))


def test_a_midnight_wrapping_window_is_judged_on_its_start() -> None:
    start, end = at(2026, 9, 15, 23, 30), at(2026, 9, 16, 0, 30)
    assert export_not_yet_armed(start, end, at(2026, 9, 15, 0, 15))
    assert not export_not_yet_armed(start, end, at(2026, 9, 15, 0, 30))
    assert not export_not_yet_armed(start, end, at(2026, 9, 15, 20, 0))
    assert export_not_yet_armed(start, end, at(2026, 9, 14, 20, 0))


def test_a_window_already_under_way_stays_armed() -> None:
    start, end = at(2026, 9, 15, 18, 30), at(2026, 9, 15, 19, 30)
    assert not export_not_yet_armed(start, end, at(2026, 9, 15, 18, 45))


def test_an_event_in_the_fall_back_repeated_hour_is_refused_not_armed() -> None:
    start = at(2026, 10, 25, 1, 30, fold=1)
    end = at(2026, 10, 25, 2, 30, fold=1)
    now = at(2026, 10, 25, 1, 0, fold=0)
    assert now.astimezone(UTC) < start.astimezone(UTC)
    assert export_not_yet_armed(start, end, now)


def test_the_discharge_half_follows_the_household_clock_face() -> None:
    start = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)  # 18:00 BST
    end = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
    assert clock_face(start, end, LONDON) == [18, 0, 19, 0]
    assert clock_face(start, end, ZoneInfo("Asia/Kolkata")) == [22, 30, 23, 30]
