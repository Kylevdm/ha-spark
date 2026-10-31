"""Commands at the result/authority seam; no socket or native hardware."""
import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import aiohttp
import probatio as vol
import pytest
from aiohttp.base_protocol import BaseProtocol
from aiohttp.streams import StreamReader
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL as YarlURL

from custom_components.ha_spark.channel import async_fetch_commands
from custom_components.ha_spark.command_dispatch import CommandConsumer
from custom_components.ha_spark.commands import Command, CommandResult, FetchRequest
from custom_components.ha_spark.const import DOMAIN
from custom_components.ha_spark.transitions import Preconditions, TransitionDecision
from tests_ha.test_owner_control import Writer
from tests_ha.test_polling import URL, fixture, setup

FETCH = URL.replace("heartbeat", "commands/fetch")
RESULT = URL.replace("heartbeat", "commands/result")


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to("2026-10-31T12:00:00+00:00")


@pytest.fixture
def hass_storage() -> dict[str, Any]:
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    return str(tmp_path)


def command(sequence: int = 1, epoch: int = 4, session: str = "addon") -> Command:
    return Command(session_id=session, epoch=epoch, sequence=sequence, kind="sync_clock",
                   payload={"requester_kind": "cli"})


async def test_refusal_consumes_sequence_and_retries_return_stored_result() -> None:
    calls: list[int] = []

    async def handle(request: Command) -> CommandResult:
        calls.append(request.sequence)
        return CommandResult(session_id=request.session_id, sequence=request.sequence,
                             outcome="refused", reason="not_available")

    consumer = CommandConsumer("addon", lambda: 4, handle)
    result = await consumer.async_consume(command())
    assert result.reason == "not_available"
    assert await consumer.async_consume(command()) == result
    assert calls == [1]
    stale = await consumer.async_consume(command(2, epoch=3))
    assert (stale.reason, stale.sequence) == ("stale_authority", 2)
    assert await consumer.async_consume(command(2)) == stale
    wrong_session = await consumer.async_consume(command(3, session="old"))
    assert (wrong_session.reason, wrong_session.sequence) == ("stale_session", 3)
    for seq in range(3, 68):
        await consumer.async_consume(command(seq))
    replay = await consumer.async_consume(command())
    assert (replay.reason, replay.sequence) == ("replayed", 1)
    assert len(consumer.results) == 64


async def test_in_flight_duplicate_returns_pending_without_running_again() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    calls: list[int] = []

    async def handle(request: Command) -> CommandResult:
        calls.append(request.sequence)
        started.set()
        await finish.wait()
        return CommandResult(session_id=request.session_id, sequence=request.sequence,
                             outcome="applied")

    consumer = CommandConsumer("addon", lambda: 4, handle)
    task = asyncio.create_task(consumer.async_consume(command()))
    await started.wait()
    assert (await consumer.async_consume(command())).outcome == "pending"
    finish.set()
    assert (await task).outcome == "applied"
    assert calls == [1]


async def test_data_free_nudges_coalesce_one_in_flight_and_one_queued(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def respond(_method: str, _url: Any, data: Any) -> AiohttpClientMockResponse:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return AiohttpClientMockResponse("post", YarlURL(FETCH), json={
            "session_id": entry.data["session_id"], "commands": [],
        })

    aioclient_mock.post(FETCH, side_effect=respond)
    coordinator.connected = True
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await started.wait()
        for _ in range(20):
            await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        assert calls == 1
        finish.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert calls == 2
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(DOMAIN, "commands_pending", {"command": {}},
                                           blocking=True)


async def test_heartbeat_collects_transition_and_waits_for_site_barrier(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    control = coordinator.control
    assert await coordinator.authority.async_initialise()
    before = coordinator.authority.state
    assert before is not None
    barrier = asyncio.Event()
    started = asyncio.Event()

    class BusyWriter(Writer):
        async def async_barrier(self) -> None:
            started.set()
            await barrier.wait()

    control.writer = BusyWriter()
    control.preconditions = lambda: Preconditions(True, True, True)
    body = fixture("plan_summary_v4")
    body["loop_tick"] = dt_util.utcnow().isoformat()
    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, json=body)
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        assert await control.async_new_session(entry.data["session_id"], now=dt_util.utcnow())
        state = coordinator.authority.state
        assert state is not None
        epoch = state.epoch
        cmd = fixture("command_transition")
        cmd.update(session_id=entry.data["session_id"], epoch=epoch)
        cmd["payload"].update(expected_epoch=epoch)
        aioclient_mock.post(FETCH, json={"session_id": entry.data["session_id"], "commands": [cmd]})
        aioclient_mock.post(RESULT, json={"accepted": True})
        await coordinator.async_refresh()
        assert coordinator.connected
        await hass.async_block_till_done()
        assert started.is_set(), aioclient_mock.mock_calls
        assert coordinator.authority.state is not None
        assert coordinator.authority.state.epoch == epoch
        assert coordinator.authority.state.committed_mode == "observe"
        barrier.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.authority.state.epoch == epoch + 1
        assert coordinator.authority.state.committed_mode == "protect"
        # Lost-result retries return the stored result, not a second transition.
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.authority.state.epoch == epoch + 1
        results = [call[2] for call in aioclient_mock.mock_calls if str(call[1]) == RESULT]
        assert len(results) == 2 and results[0] == results[1]
        assert results[0]["outcome"] == "applied"
        # Manual start and resume use the very same fetched controller path.
        manual = fixture("command_manual")
        manual.update(session_id=entry.data["session_id"], epoch=epoch + 1)
        manual["payload"]["expected_epoch"] = epoch + 1
        aioclient_mock.clear_requests()
        aioclient_mock.post(RESULT, json={"accepted": True})
        aioclient_mock.post(FETCH, json={"session_id": entry.data["session_id"],
                                        "commands": [manual]})
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        state = coordinator.authority.state
        assert state is not None and state.manual_session is not None
        assert state.epoch == epoch + 2
        assert state.manual_session.deadline is not None
        manual.update(sequence=3, epoch=epoch + 2)
        manual["payload"].update(target="resume", expected_epoch=epoch + 2)
        aioclient_mock.clear_requests()
        aioclient_mock.post(RESULT, json={"accepted": True})
        aioclient_mock.post(FETCH, json={"session_id": entry.data["session_id"],
                                        "commands": [manual]})
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        state = coordinator.authority.state
        assert state is not None and state.manual_session is None
        assert state.epoch == epoch + 3


@pytest.mark.parametrize("kind", ["apply", "sync_clock", "reprobe", "supervise_export",
                                  "end_supervision"])
async def test_unavailable_kinds_are_validated_refused_and_consumed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kind: str,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    cmd = fixture(f"command_{kind}")
    cmd.update(epoch=state.epoch, session_id=entry.data["session_id"])
    aioclient_mock.post(FETCH, json={"session_id": entry.data["session_id"], "commands": [cmd]})
    aioclient_mock.post(RESULT, json={"accepted": True})
    coordinator.connected = True
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        for _ in range(2):
            await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
            await hass.async_block_till_done(wait_background_tasks=True)
    results = [call[2] for call in aioclient_mock.mock_calls if str(call[1]) == RESULT]
    assert len(results) == 2 and results[0] == results[1], aioclient_mock.mock_calls
    assert results[0]["reason"] == "not_available"
    assert results[0]["sequence"] == cmd["sequence"]
    assert coordinator.authority.state == state


async def test_failed_authority_advance_cannot_report_applied(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    cmd = fixture("command_transition")
    cmd.update(epoch=state.epoch, session_id=entry.data["session_id"])
    cmd["payload"]["expected_epoch"] = state.epoch
    aioclient_mock.post(FETCH, json={"session_id": entry.data["session_id"], "commands": [cmd]})
    aioclient_mock.post(RESULT, json={"accepted": True})
    coordinator.connected = True
    # A granted request can lose its verified store while advancing device steps.
    with (
        patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"),
        patch.object(
            coordinator.control, "async_request", return_value=TransitionDecision(),
        ) as grant,
    ):
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert grant.await_args is not None
        carried = grant.await_args.args[0]
        assert carried.requester_kind == "ingress" and carried.user_id == "owner-id"
    results = [call[2] for call in aioclient_mock.mock_calls if str(call[1]) == RESULT]
    assert len(results) == 1
    assert results[0]["outcome"] == "refused"
    assert results[0]["reason"] == "authority_unknown"


async def test_restart_mints_a_new_receiver_incarnation_without_persisting_sequences(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    receiver = entry.runtime_data.integration_session_id
    addon_session = entry.data["session_id"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.integration_session_id != receiver
    assert entry.data["session_id"] == addon_session
    assert entry.runtime_data.command_consumer is None


@pytest.mark.parametrize("stale", ["command_session", "batch_session", "authority",
                                   "payload_epoch"])
async def test_fetched_stale_commands_return_named_sequence_without_changing_authority(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, stale: str,
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientError())
    entry = await setup(hass)
    coordinator = entry.runtime_data
    assert await coordinator.authority.async_initialise()
    state = coordinator.authority.state
    assert state is not None
    cmd = fixture("command_transition")
    cmd.update(epoch=state.epoch, session_id=entry.data["session_id"])
    cmd["payload"]["expected_epoch"] = state.epoch
    batch = {"session_id": entry.data["session_id"], "commands": [cmd]}
    if stale == "command_session":
        cmd["session_id"] = "old-session"
    elif stale == "batch_session":
        batch["session_id"] = "old-session"
    elif stale == "authority":
        cmd["epoch"] = state.epoch + 1
    else:
        # An envelope at the store epoch cannot carry a later expected epoch.
        cmd["payload"]["expected_epoch"] = state.epoch + 1
    aioclient_mock.post(FETCH, json=batch)
    aioclient_mock.post(RESULT, json={"accepted": True})
    coordinator.connected = True
    with patch("custom_components.ha_spark.authority.authority_owner", return_value="integration"):
        await hass.services.async_call(DOMAIN, "commands_pending", {}, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
    results = [call[2] for call in aioclient_mock.mock_calls if str(call[1]) == RESULT]
    assert len(results) == 1
    assert results[0]["reason"] == ("stale_session" if stale.endswith("session")
                                    else "stale_authority")
    assert results[0]["sequence"] == cmd["sequence"]
    assert coordinator.authority.state == state


@pytest.mark.parametrize("case", ["oversize", "redirect", "malformed", "too_many"])
async def test_fetch_client_fails_closed_without_following_redirects_or_logging_payloads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture, case: str,
) -> None:
    if case == "redirect":
        aioclient_mock.post(FETCH, status=302, headers={"Location": "http://elsewhere.test/"})
    elif case == "oversize":
        aioclient_mock.post(FETCH, text=" " * 65537)
    elif case == "too_many":
        aioclient_mock.post(FETCH, json={"session_id": "addon-session", "commands": [
            {**fixture("command_sync_clock"), "sequence": index + 1} for index in range(17)
        ]})
    else:
        aioclient_mock.post(FETCH, text="COMMAND-CANARY")
    reply = await async_fetch_commands(
        async_get_clientsession(hass), url=FETCH, token="TOKEN-CANARY",
        request=FetchRequest(session_id="addon-session", integration_session_id="receiver",
                             last_result_sequence=0),
    )
    assert reply is None
    assert aioclient_mock.call_count == 1
    assert "COMMAND-CANARY" not in caplog.text and "TOKEN-CANARY" not in caplog.text


async def test_command_fetch_streams_partial_chunks(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker,
) -> None:
    body = json.dumps({"session_id": "addon-session", "commands": [
        fixture("command_sync_clock"),
    ]}).encode()
    protocol = BaseProtocol(asyncio.get_running_loop())
    protocol.connection_made(Mock())
    stream = StreamReader(protocol, limit=2**16)
    stream.feed_data(body[:10])

    async def rest() -> None:
        await asyncio.sleep(0)
        stream.feed_data(body[10:])
        stream.feed_eof()

    class Partial(AiohttpClientMockResponse):  # type: ignore[misc]
        @property
        def content(self) -> StreamReader:
            return stream

    async def respond(_method: str, _url: Any, _data: Any) -> AiohttpClientMockResponse:
        hass.async_create_task(rest())
        return Partial("post", YarlURL(FETCH))

    aioclient_mock.post(FETCH, side_effect=respond)
    reply = await async_fetch_commands(
        async_get_clientsession(hass), url=FETCH, token="unused",
        request=FetchRequest(session_id="addon-session", integration_session_id="receiver",
                             last_result_sequence=0),
    )
    assert reply is not None and reply.commands[0].kind == "sync_clock"
