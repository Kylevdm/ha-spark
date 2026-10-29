"""Every fake charge write is clamped at the serialised boundary."""

import asyncio
import json
from pathlib import Path

import pytest

from custom_components.ha_spark.clamp import ClampedWriter, DeviceIntent, clamp_intent
from custom_components.ha_spark.topology import Topology, effective_limits


def topology() -> Topology:
    path = Path(__file__).parents[1] / "tests/fixtures/limits/two_inverters.json"
    raw = json.loads(path.read_text())
    return Topology.model_validate(raw["topology"])


class FakeWriter:
    """No hardware: record only the intents crossing the final boundary."""

    def __init__(self) -> None:
        self.intents: list[DeviceIntent] = []

    async def async_write(self, intent: DeviceIntent) -> None:
        self.intents.append(intent)


def test_proportional_share_rounds_down() -> None:
    site = topology()
    a, b = site.inverters
    bank = site.banks[0]
    intent = clamp_intent(DeviceIntent(a.id, bank.id, 40), allowance_kw=3,
                          last_requested_a={a.id: 40, b.id: 20}, topology=site,
                          limits=effective_limits(site))
    assert intent is not None
    assert intent.charge_a == int(2000 / bank.nominal_voltage_v)


@pytest.mark.parametrize("cut_first", [True, False])
async def test_concurrent_apply_and_cut(cut_first: bool) -> None:
    site = topology()
    bank = site.banks[0]
    inv = site.inverters[0]

    class Guard:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.topology = site
            self.limits = effective_limits(site)
            self.last_requested_a: dict[str, float] = {inv.id: 40}
            self.allowance = 1.0

        def allowance_for(self, bank_id: str) -> float | None:
            return self.allowance

        def permits_writes(self) -> bool:
            return True

    guard = Guard()

    class CheckingWriter(FakeWriter):
        async def async_write(self, intent: DeviceIntent) -> None:
            # Suspend inside the final write, giving the competing operation a
            # chance to run. Its cut must wait rather than change the ceiling
            # after the apply was clamped but before it crosses the boundary.
            await asyncio.sleep(0)
            assert intent.charge_a * bank.nominal_voltage_v <= guard.allowance * 1000
            await super().async_write(intent)

    fake = CheckingWriter()
    writer = ClampedWriter(fake, guard)

    async def cut() -> None:
        async with guard.lock:
            guard.allowance = 0.5
            await writer.async_write_locked(DeviceIntent(inv.id, bank.id, 40))

    async def apply() -> None:
        await writer.async_write(DeviceIntent(inv.id, bank.id, 40))

    await asyncio.gather(*([cut(), apply()] if cut_first else [apply(), cut()]))
    assert all(i.charge_a * bank.nominal_voltage_v <= 1000 for i in fake.intents)
    await writer.async_write(DeviceIntent(inv.id, bank.id, 40))
    assert fake.intents[-1].charge_a * bank.nominal_voltage_v <= 500


def test_alphaess_has_no_charge_command() -> None:
    site = topology()
    inv = site.inverters[0].model_copy(update={"driver_kind": "alphaess"})
    site = site.model_copy(update={"inverters": (inv,)})
    assert clamp_intent(DeviceIntent(inv.id, inv.bank_id, 40), allowance_kw=1,
                        last_requested_a={inv.id: 40}, topology=site,
                        limits=effective_limits(site)) is None


@pytest.mark.parametrize("allowance", [0, 0.01, 0.5, 2, 100, None])
def test_clamp_never_widens_or_starts(allowance: float | None) -> None:
    site = topology()
    inv = site.inverters[0]
    for request in (0, 0.9, 20.5, 100):
        result = clamp_intent(DeviceIntent(inv.id, inv.bank_id, request), allowance_kw=allowance,
                              last_requested_a={inv.id: request}, topology=site,
                              limits=effective_limits(site))
        assert result is not None
        assert 0 <= result.charge_a <= min(request, 62.5)
        assert result.charge_a == int(result.charge_a)
        if allowance is not None:
            assert result.charge_a * site.banks[0].nominal_voltage_v <= allowance * 1000


def test_v2l_is_not_grid_draw() -> None:
    from custom_components.ha_spark.topology import V2LCharger

    site = topology()
    inv = site.inverters[0]
    intent = DeviceIntent(inv.id, inv.bank_id, 40)
    other = site.model_copy(update={"v2l_chargers": (
        V2LCharger(id="car", bank_id=inv.bank_id, max_current_a=50),
    )})
    results = [clamp_intent(intent, allowance_kw=1, last_requested_a={inv.id: 40}, topology=s,
                            limits=effective_limits(s)) for s in (site, other)]
    assert results[0] == results[1]
