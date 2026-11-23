"""The manual SoC-trust override reaches only the owner's own edits (#766 review)."""
from types import SimpleNamespace

import pytest

from custom_components.ha_spark.authority_store import ManualSession
from custom_components.ha_spark.commands import IntentResult
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_write import command, hass_config_dir, hass_storage  # noqa: F401
from tests_ha.test_native_write import writer as writer_fixture

writer = writer_fixture


@pytest.fixture
async def second(writer: SparkCoordinator, native_transport: FakeSolisDevice,
                 monkeypatch: pytest.MonkeyPatch) -> FakeSolisDevice:
    device = FakeSolisDevice()
    device.set_identity()
    device.set_input(33132, [32])
    device.set_holding(43141, [0])
    device.traffic = native_transport.traffic
    monkeypatch.setattr(writer.native, "transport", lambda inv: (
        native_transport if inv.gateway_unit_id == 1 else device))
    await writer.async_reprobe("inv2")
    return device


async def override_session(c: SparkCoordinator, monkeypatch: pytest.MonkeyPatch) -> None:
    state = c.authority.state
    assert state is not None
    assert await c.authority.async_save(state.model_copy(update={"manual_session": ManualSession(
        deadline=None, return_to="optimise", keep_protection=False, override_soc_trust=True)}))
    c.control.ready = True
    monkeypatch.setattr(c, "soc", SimpleNamespace(increases_allowed=lambda bank: False))


async def owner_current(c: SparkCoordinator, inverter_id: str, amps: float) -> IntentResult:
    state = c.authority.state
    assert state is not None
    return await c.writes.async_entity(inverter_id, "charge", amps, epoch=state.epoch)


async def guard(c: SparkCoordinator, requests: dict[str, float], *,
                cut_only: set[str] | None = None) -> IntentResult:
    state = c.authority.state
    assert state is not None and c.guard is not None
    async with c.guard.lock:
        return await c.writes.async_guard_locked(requests, epoch=state.epoch, cut_only=cut_only)


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_override_never_raises_a_peer_the_owner_did_not_edit(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, second: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale plan request on a peer stays refused while its bank SoC is untrusted."""
    request = command(writer, 40)
    request.payload = {"intents": [{"kind": "charge_current_limit",
                                    "inverter_id": "inv2", "current_a": 40.0}]}
    assert (await writer._async_dispatch_command(request)).outcome == "applied"
    second.set_holding(43141, [100])  # a later guard cut, the 40 A request still latent
    await override_session(writer, monkeypatch)

    result = await owner_current(writer, "inv1", 15)

    # The peer's refused raise stops the transaction, as any refused peer does.
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert second.holding[43141] == 100


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_guard_restores_only_up_to_the_owners_edit(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, second: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a cut, the restore may raise to the owner's edited current, never past it."""
    await override_session(writer, monkeypatch)
    assert (await owner_current(writer, "inv1", 30)).outcome == "applied"
    assert native_transport.holding[43141] == 300

    await guard(writer, {"inv1": 10, "inv2": 0}, cut_only={"inv1", "inv2"})
    assert native_transport.holding[43141] == 100

    above = await guard(writer, {"inv1": 31, "inv2": 0})
    assert (above.outcome, above.reason) == ("refused", "untrusted_soc")
    assert native_transport.holding[43141] == 100

    restored = await guard(writer, {"inv1": 30, "inv2": 0})
    assert restored.outcome in ("applied", "unchanged")
    assert native_transport.holding[43141] == 300

    peer = await guard(writer, {"inv1": 30, "inv2": 20})
    assert (peer.outcome, peer.reason) == ("refused", "untrusted_soc")
    assert second.holding[43141] == 0


@pytest.mark.parametrize("writer", [True], indirect=True)
async def test_a_refused_owner_edit_is_not_a_restore_target(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, second: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edit refused before its write never lets a later restore through."""
    await override_session(writer, monkeypatch)
    state = writer.authority.state
    assert state is not None
    stale = await writer.writes.async_entity("inv1", "charge", 30, epoch=state.epoch - 1)
    assert stale.reason == "stale_epoch"

    result = await guard(writer, {"inv1": 30, "inv2": 0})
    assert (result.outcome, result.reason) == ("refused", "untrusted_soc")
    assert native_transport.holding[43141] == 100


async def test_owner_discharge_edits_are_not_soc_gated(
    writer: SparkCoordinator, native_transport: FakeSolisDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discharge edits never needed the override: untrusted SoC does not gate them."""
    state = writer.authority.state
    assert state is not None
    assert await writer.authority.async_save(state.model_copy(update={
        "manual_session": ManualSession(
            deadline=None, return_to="optimise", keep_protection=False)}))
    writer.control.ready = True
    monkeypatch.setattr(writer, "soc", SimpleNamespace(increases_allowed=lambda bank: False))
    native_transport.set_holding(43142, [0])
    native_transport.set_holding(43143, [23, 30, 5, 30, 0, 0, 0, 0])

    current = await writer.writes.async_entity("inv1", "discharge", 10, epoch=state.epoch)
    window = await writer.writes.async_entity("inv1", "discharge", [16, 0], epoch=state.epoch,
                                              offset=4)

    assert (current.outcome, window.outcome) == ("applied", "applied")
