"""Command queue behaviour, independent of hardware and HTTP."""
from unittest.mock import AsyncMock

import pytest

from ha_spark import cli
from ha_spark.api.commands import Command, CommandQueue, CommandResult, FetchReply, FetchRequest
from ha_spark.config import Settings
from ha_spark.daemon_channel import Reply


def fetch(receiver="receiver"):
    return FetchRequest(session_id="addon", integration_session_id=receiver, last_result_sequence=0)


def test_unacknowledged_commands_are_reoffered_in_bounded_batches():
    queue = CommandQueue("addon")
    for _ in range(18):
        queue.enqueue(epoch=1, kind="sync_clock", payload={"requester_kind": "cli"})
    first = queue.fetch(fetch())
    assert len(first.commands) == 16
    assert queue.fetch(fetch()) == first
    queue.accept(CommandResult(session_id="addon", sequence=1, outcome="refused",
                               reason="not_available"))
    assert queue.fetch(fetch()).commands[0].sequence == 2


def test_new_receiver_session_drops_earlier_commands():
    queue = CommandQueue("addon")
    queue.fetch(fetch())
    queue.enqueue(epoch=1, kind="sync_clock", payload={"requester_kind": "cli"})
    assert queue.fetch(fetch("restarted")).commands == []


async def test_wait_timeout_retains_work_without_resubmitting(monkeypatch):
    import asyncio

    queue = CommandQueue("addon")
    request = queue.enqueue(epoch=1, kind="sync_clock", payload={"requester_kind": "cli"})
    original = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: original(0))
    assert await queue.wait(request.sequence) is None
    assert list(queue.pending) == [request.sequence]
    assert queue.sequence == 1


@pytest.mark.parametrize("change", [
    {"epoch": True}, {"epoch": 0}, {"sequence": "1"}, {"sequence": -1},
    {"session_id": ""}, {"kind": "CANARY"}, {"payload": {"requester_kind": "system"}},
    {"payload": {"requester_kind": "ingress"}}, {"unexpected": "CANARY"},
])
def test_envelope_is_strict(change):
    body = {"session_id": "addon", "epoch": 1, "sequence": 1,
            "kind": "sync_clock", "payload": {"requester_kind": "cli"}}
    with pytest.raises(ValueError):
        Command.model_validate(body | change)


def test_batches_reject_out_of_order_or_more_than_sixteen():
    queue = CommandQueue("addon")
    commands = [queue.enqueue(epoch=1, kind="sync_clock", payload={"requester_kind": "cli"})
                for _ in range(17)]
    for batch in (commands, list(reversed(commands[:2]))):
        with pytest.raises(ValueError):
            FetchReply(session_id="addon", commands=batch)


@pytest.mark.parametrize("target", ["manual", "resume"])
def test_cli_manual_and_resume_submit_once_with_seen_epoch(tmp_path, monkeypatch, target):
    settings = Settings(ha_url="http://ha.test", ha_token="unused", db_path=str(tmp_path / "db"))
    submit = AsyncMock(side_effect=[Reply(authority={"epoch": 4}), Reply(error="pending")])
    monkeypatch.setattr(cli, "submit_socket", submit)
    monkeypatch.setattr(cli, "load_settings", lambda **kwargs: settings)
    assert cli.main(["mode", target]) == 1
    assert submit.await_count == 2
    assert submit.await_args_list[1].kwargs == {
        "target": target, "expected_epoch": 4, "duration": "one_hour", "keep_protection": True,
    }


def test_result_must_name_an_offered_sequence_and_session():
    queue = CommandQueue("addon")
    with pytest.raises(ValueError):
        queue.accept(CommandResult(session_id="addon", sequence=3, outcome="applied"))
