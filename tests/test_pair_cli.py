"""Owner re-pair requests travel once over the private daemon socket."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from ha_spark.api import site_pairing
from ha_spark.api.server import AppState
from ha_spark.authority import AuthorityStore, Transition
from ha_spark.cli import _cmd_pair_replace, main
from ha_spark.config import Settings
from ha_spark.daemon_channel import DaemonChannel, Reply, submit_socket
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import load_topology_record, save_topology_record

OLD = "00000000-0000-0000-0000-000000000001"
NEW = "00000000-0000-0000-0000-000000000002"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("ha_spark.authority.resolve_data_dir", lambda _: tmp_path)
    monkeypatch.setattr("ha_spark.daemon_channel.resolve_data_dir", lambda _: tmp_path)
    return Settings(supervisor_token="unused", db_path=str(tmp_path / "spark.db"))


@pytest.mark.parametrize("reason", [None, *site_pairing.REPLACE_TEXT])
async def test_pair_replace_socket_results(settings, monkeypatch, capsys, reason):
    raw = json.loads((Path(__file__).parent / "fixtures/limits/owner_below_driver_ceiling.json")
                     .read_text())
    topology = Topology.model_validate(raw["topology"])
    save_topology_record(settings, 9, topology, (9, topology))
    assert site_pairing.save_pairing(settings, OLD)
    old, new = OLD, NEW
    if reason == "invalid_site_id":
        new = NEW.replace("-", "")  # passes the socket bound, not canonical
    elif reason == "same_site":
        new = OLD
    elif reason == "pairing_mismatch":
        old, new = NEW, OLD
    elif reason == "pairing_untrusted":
        (Path(settings.db_path).parent / site_pairing.PAIRING_FILE).write_bytes(b"broken")
    elif reason in ("mode_not_observe", "transition_pending"):
        store = AuthorityStore(settings)
        store.initialise()
        update = {"committed_mode": "protect"} if reason == "mode_not_observe" else {
            "transition": Transition(
                target="protect", started_at=datetime(2026, 10, 24, tzinfo=UTC)
            )
        }
        store.save(store.state.model_copy(update=update))
    elif reason == "handshake_pending":
        monkeypatch.setattr("ha_spark.api.server.rollback_pending", lambda _: True)
    elif reason == "pairing_save_failed":
        monkeypatch.setattr(site_pairing, "replace_pairing", lambda *_: "pairing_save_failed")
    monkeypatch.setattr("builtins.input", lambda _: new)
    async with DaemonChannel(settings) as channel:
        AppState(settings, Path(settings.db_path).parent / "options.json", executor=channel)
        if reason == "invalid_site_id":
            # The CLI refuses locally and sends nothing; the daemon refuses the same id too.
            assert await _cmd_pair_replace(settings, old, new) == 2
            reply = await submit_socket(settings, "pair_replace", from_site=old, to_site=new)
            assert reply is not None
            assert reply.error == reason and reply.text == site_pairing.REPLACE_TEXT[reason]
            print(f"[REFUSED] {reply.error}: {reply.text}")
        else:
            assert await _cmd_pair_replace(settings, old, new) == (0 if reason is None else 1)
        assert channel.next_request() is None
    output = capsys.readouterr().out
    if reason is None:
        assert site_pairing.load_pairing(settings) == (NEW, True)
        assert "next snapshot" in output
        assert load_topology_record(settings).topology == topology
    else:
        assert f"[REFUSED] {reason}: {site_pairing.REPLACE_TEXT[reason]}" in output
        # Fixed text only: neither submitted id is echoed back.
        assert old not in output and new not in output
        if reason == "pairing_save_failed":
            assert "ha-spark health" in output


@pytest.mark.parametrize("confirmation", ["wrong", None])
async def test_confirmation_sends_nothing(settings, monkeypatch, capsys, confirmation):
    assert site_pairing.save_pairing(settings, OLD)
    before = (Path(settings.db_path).parent / site_pairing.PAIRING_FILE).read_bytes()

    def confirm(_):
        if confirmation is None:
            raise EOFError
        return confirmation

    monkeypatch.setattr("builtins.input", confirm)
    submit = AsyncMock()
    monkeypatch.setattr("ha_spark.cli.submit_socket", submit)
    assert await _cmd_pair_replace(settings, OLD, NEW) == 2
    submit.assert_not_awaited()
    assert (Path(settings.db_path).parent / site_pairing.PAIRING_FILE).read_bytes() == before
    assert "nothing was sent" in capsys.readouterr().out


async def test_lost_pair_reply_never_retries(settings, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: NEW)
    submit = AsyncMock(return_value=Reply(
        error="daemon response unavailable; request may have executed"
    ))
    monkeypatch.setattr("ha_spark.cli.submit_socket", submit)
    assert await _cmd_pair_replace(settings, OLD, NEW) == 1
    submit.assert_awaited_once_with(settings, "pair_replace", from_site=OLD, to_site=NEW)
    output = capsys.readouterr().out
    assert "unknown" in output and "ha-spark health" in output


async def test_pair_daemon_unreachable(settings, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: f"  {NEW}  ")
    submit = AsyncMock(return_value=None)
    monkeypatch.setattr("ha_spark.cli.submit_socket", submit)
    assert await _cmd_pair_replace(settings, OLD, NEW) == 1
    submit.assert_awaited_once()
    assert "no ha-spark daemon running" in capsys.readouterr().out


def test_pair_cli_dispatch(settings, monkeypatch):
    monkeypatch.setattr("ha_spark.cli.load_settings", lambda **_: settings)
    monkeypatch.setattr("builtins.input", lambda _: NEW)
    submit = AsyncMock(return_value=Reply(text="pairing replaced"))
    monkeypatch.setattr("ha_spark.cli.submit_socket", submit)
    assert main(["pair", "--replace", "--from", OLD, "--to", NEW]) == 0
    submit.assert_awaited_once_with(settings, "pair_replace", from_site=OLD, to_site=NEW)


@pytest.mark.parametrize("old,new", [(OLD, NEW + "0"), ("not-a-site", NEW), (OLD, NEW[:-1])])
async def test_non_canonical_ids_send_nothing(settings, monkeypatch, capsys, old, new):
    confirm = Mock()
    monkeypatch.setattr("builtins.input", confirm)
    submit = AsyncMock()
    monkeypatch.setattr("ha_spark.cli.submit_socket", submit)
    assert await _cmd_pair_replace(settings, old, new) == 2
    submit.assert_not_awaited()
    confirm.assert_not_called()
    assert "invalid_site_id" in capsys.readouterr().out
