"""Real daemon plan ticks across supplier, allowance, planner and command seams (#702).

Only external I/O is faked: HA REST/recorder, Octopus HTTP, the household
clock, the data directory and the integration's command-channel receiver.
"""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from websockets.asyncio.server import serve

from ha_spark.api.commands import CommandQueue, CommandResult, FetchRequest
from ha_spark.api.native_apply import DatedChargeWindow, NativeApply
from ha_spark.config import Settings
from ha_spark.devices.execution import ExecutionContext
from ha_spark.devices.outcomes import ActionOutcome
from ha_spark.energy import forecast, octopus, scheduler, soc_monitor, sources
from ha_spark.energy.solar_source import SolarSource
from ha_spark.energy.tariff import HAPPY_HOUR_ALLOWANCE_KWH, fixed_schedule
from tests.authority_helpers import seed_authority
from tests.native_helpers import NOW, native_link
from tests.octopus_helpers import token_response
from tests.test_statistics import _handler

END = NOW + timedelta(hours=1)
GRAPHQL = "http://octo.test/v1/graphql/"


@pytest.fixture
async def tick_site(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    for module in (scheduler, sources, octopus, forecast, soc_monitor):
        monkeypatch.setattr(module, "datetime", Clock)

    # The configured cumulative meter also enables the rolling base-load
    # rerive. Answer its real recorder protocol, rather than patching a helper.
    requests = []
    history = [
        {"start": (NOW.replace(hour=0) - timedelta(hours=h)).timestamp() * 1000,
         "change": 1.5}
        for h in range(1, 24 * 28 + 1)
    ]

    def recorder(request):
        rows = history if request.get("statistic_ids") == ["sensor.house_load"] else []
        return {"success": True, "result": {
            entity: rows for entity in request.get("statistic_ids", [])
        }}

    handler = _handler(recorder, requests)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        settings = Settings(
            ha_url=f"http://127.0.0.1:{port}", ha_token="test",
            db_path=str(tmp_path / "tick.db"), timezone="UTC",
            tariff_provider="fixed", expected_load_kwh=36,
            battery_capacity_kwh=10, battery_voltage_v=50, charge_efficiency=1,
            octopus_api_url="http://octo.test/v1",
            octopus_backend_api_url="http://octo.test/v1",
            octopus_api_key="sk_test_fake", octopus_account_number="A-1234ABCD",
            octopus_mpan="test-mpan", derive_grid_import_entity="sensor.grid_import",
            soc_entity="sensor.soc", consumption_energy_entity="sensor.house_load",
            load_model="median",
        )
        with respx.mock(assert_all_called=False) as router:
            def state(request):
                entity = request.url.path.rsplit("/", 1)[-1]
                values = {"sensor.grid_import": "100", "sensor.soc": "42"}
                if entity not in values:
                    return httpx.Response(404)
                return httpx.Response(200, json={
                    "entity_id": entity, "state": values[entity],
                    "attributes": {"unit_of_measurement": "kWh"},
                    "last_updated": NOW.isoformat(), "last_reported": NOW.isoformat(),
                })

            router.get(url__regex=settings.ha_rest_url + r"/states/.*").mock(side_effect=state)
            router.get(settings.ha_rest_url + "/config").respond(
                200, json={"latitude": 51.5, "longitude": -0.1, "time_zone": "UTC"})
            yield settings, router


def _supplier(router, kind):
    def reply(request):
        query = json.loads(request.content)["query"]
        if "obtainKrakenToken" in query:
            return token_response()
        if kind == "failed":
            return httpx.Response(503)
        if "customerFlexibilityCampaignEvents" in query:
            edges = ([{"node": {"code": "free-session", "startAt": NOW.isoformat(),
                                "endAt": END.isoformat()}}] if kind == "free" else [])
            return httpx.Response(200, json={"data": {
                "isEnrolledInCustomerFlexibilityCampaign": True,
                "customerFlexibilityCampaignEvents": {"edges": edges},
            }})
        assert "HappyHours" in query
        events = ([{"eventId": "8066", "eventType": "WEEKEND_HAPPY_HOUR",
                    "startAt": NOW.isoformat(), "endAt": END.isoformat()}]
                  if kind == "happy" else [])
        return httpx.Response(200, json={"data": {"savingSessions": {"account": {
            "joinedEvents": events,
        }}}})

    router.post(GRAPHQL).mock(side_effect=reply)


async def _tick(settings, monkeypatch, *, native=True, mode="observe", api_version=5):
    context = ExecutionContext()
    store = seed_authority(settings, mode, context=context)
    queue = CommandQueue("test-session")
    link = native_link(settings)
    link = replace(link, snapshot=link.snapshot.model_copy(update={"api_version": api_version}))
    if native:
        assert store.handover("pending_integration") == "retired"
        context.limits_record = link.topology
    sent = []

    async def wait(sequence):
        # Act as the integration receiver on the public command channel.
        queue.fetch(FetchRequest(session_id=queue.session_id,
                                 integration_session_id="receiver", last_result_sequence=0))
        sent.append(queue.pending[sequence])
        queue.accept(CommandResult(session_id=queue.session_id, sequence=sequence,
                                   outcome="applied", epoch=link.authority_epoch))
        return queue.results[sequence]

    monkeypatch.setattr(queue, "wait", wait)
    result = await scheduler.run_once(
        settings, execution=context,
        source=SolarSource("entities", None, None, NOW),
        native_apply=NativeApply(queue), native_link=link,
        native_epoch=link.authority_epoch,
    )
    return result, sent, link


def _purchase(result, kind):
    [purchase] = result.plan.daytime_purchases
    assert not purchase.refused
    assert purchase.energy_kwh > 0
    assert purchase.overlay == kind
    assert (purchase.start, purchase.end) == (NOW, END)
    run = result.plan_run.run
    assert run.schedule.import_overlays[:2] == (kind, kind)
    if kind == "happy_hour":
        assert run.inputs.happy_hour_remaining_kwh == {"8066": HAPPY_HOUR_ALLOWANCE_KWH}
    return purchase


def _dated(sent, link):
    [command] = sent
    assert command.kind == "apply"
    assert command.epoch == link.authority_epoch == 7
    return [intent for intent in command.payload["intents"] if intent["kind"] == "dated_charge"]


def _allowance(settings):
    with sqlite3.connect(settings.db_path) as db:
        [row] = db.execute(
            "SELECT session_id, session_start, session_end, baseline_kwh, latest_kwh, "
            "trust_reason FROM happy_hour_allowance"
        ).fetchall()
    assert row[0] == "8066"
    assert tuple(map(datetime.fromisoformat, row[1:3])) == (NOW, END)
    assert row[3:] == (100.0, 100.0, "")


async def test_happy_hour_tick_records_allowance_and_sends_dated_intent(tick_site, monkeypatch):
    settings, router = tick_site
    _supplier(router, "happy")
    result, sent, link = await _tick(settings, monkeypatch)
    _purchase(result, "happy_hour")
    _allowance(settings)
    [raw] = _dated(sent, link)
    window = DatedChargeWindow.model_validate(raw)
    assert (window.start, window.end) == (NOW, END)
    assert window.current_a > 0
    assert result.applied_plan is result.plan


async def test_free_electricity_tick_sends_dated_intent(tick_site, monkeypatch):
    settings, router = tick_site
    _supplier(router, "free")
    result, sent, link = await _tick(settings, monkeypatch)
    _purchase(result, "free_electricity")
    [raw] = _dated(sent, link)
    window = DatedChargeWindow.model_validate(raw)
    assert (window.start, window.end) == (NOW, END)
    assert window.current_a > 0
    assert result.applied_plan is result.plan


async def test_failed_supplier_read_keeps_ordinary_tariff_and_sends_untrusted(
    tick_site, monkeypatch,
):
    settings, router = tick_site
    _supplier(router, "failed")
    result, sent, link = await _tick(settings, monkeypatch)
    assert result.plan.daytime_purchases == ()
    assert result.plan.charge_intent.daytime_windows == ()
    [dated] = _dated(sent, link)
    assert dated["request"] == "untrusted"
    run = result.plan_run.run
    assert not any(run.schedule.import_overlays)
    assert run.schedule == fixed_schedule(run.inputs, run.cfg)
    assert run.schedule.prices[0] == settings.rate_peak_gbp_kwh
    with sqlite3.connect(settings.db_path) as db:
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "happy_hour_allowance"),
        ).fetchone()
        if table is not None:
            assert db.execute("SELECT count(*) FROM happy_hour_allowance").fetchone() == (0,)


async def test_observe_tick_simulates_the_same_daytime_purchase(tick_site, monkeypatch, caplog):
    settings, router = tick_site
    _supplier(router, "happy")
    with caplog.at_level("INFO", logger="ha_spark.energy.scheduler"):
        result, sent, _ = await _tick(settings, monkeypatch, native=False)
    _purchase(result, "happy_hour")
    _allowance(settings)
    assert not sent
    assert any(getattr(line, "outcome", None) == ActionOutcome.SIMULATED
               for line in result.apply_lines)
    assert "[SIMULATE]" in caplog.text
    assert not any(call.request.method == "POST" and "/api/services/" in str(call.request.url)
                   for call in router.calls)


async def test_add_on_owned_tick_names_dated_charge_refusal(tick_site, monkeypatch):
    settings, router = tick_site
    _supplier(router, "happy")
    result, sent, _ = await _tick(settings, monkeypatch, native=False, mode="protect")
    _purchase(result, "happy_hour")
    _allowance(settings)
    assert not sent
    assert "[REFUSED] daytime purchase: dated charge windows need an integration-owned " \
           "Solis site" in result.action_lines


async def test_an_older_integration_names_the_dated_charge_refusal(tick_site, monkeypatch):
    settings, router = tick_site
    _supplier(router, "happy")
    result, sent, link = await _tick(settings, monkeypatch, api_version=4)
    _purchase(result, "happy_hour")
    assert _dated(sent, link) == []
    assert "[REFUSED] daytime purchase: dated charge windows need the companion " \
           "integration updated to API version 5" in result.action_lines


@pytest.mark.parametrize(("native", "api_version"), [(False, 5), (True, 4)])
async def test_a_dated_charge_refusal_never_holds_optimise_activation(
    tick_site, monkeypatch, native, api_version,
):
    # Activation and the CLI reply treat REFUSED and FAILED lines as device actions
    # that went wrong; a purchase the site cannot arm is a plan diagnostic.
    settings, router = tick_site
    _supplier(router, "happy")
    result, _, _ = await _tick(settings, monkeypatch, native=native,
                               mode="observe" if native else "protect",
                               api_version=api_version)
    [line] = [line for line in result.action_lines if "daytime purchase" in line]
    assert line.outcome not in (ActionOutcome.REFUSED, ActionOutcome.FAILED)
