import pytest

from ha_spark.devices import registry
from ha_spark.devices.base import Capability, ControlAuthority, effective_mode


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


def test_effective_mode_only_ha_spark_passes_proactive_through():
    # ha_spark authority: proactive_mode passes through unchanged.
    assert effective_mode(ControlAuthority.HA_SPARK, "on") == "on"
    assert effective_mode(ControlAuthority.HA_SPARK, "simulate") == "simulate"
    assert effective_mode(ControlAuthority.HA_SPARK, "off") == "off"


def test_effective_mode_non_ha_spark_never_writes():
    # observe and supplier collapse to a no-write "observe", even with on.
    assert effective_mode(ControlAuthority.OBSERVE, "on") == "observe"
    assert effective_mode(ControlAuthority.SUPPLIER, "on") == "observe"
    assert effective_mode(ControlAuthority.OBSERVE, "simulate") == "observe"
    assert effective_mode(ControlAuthority.SUPPLIER, "simulate") == "observe"
    assert effective_mode(ControlAuthority.SUPPLIER, "off") == "observe"
    assert effective_mode(ControlAuthority.OBSERVE, "off") == "observe"


def test_capability_and_authority_values():
    assert Capability.CHARGE_RATE == "charge_rate"
    assert set(ControlAuthority) == {
        ControlAuthority.OBSERVE,
        ControlAuthority.HA_SPARK,
        ControlAuthority.SUPPLIER,
    }


def test_registry_register_and_lookup():
    registry._REGISTRY.clear()

    @registry.register("dummy")
    class Dummy:
        pass

    assert registry.lookup("dummy") is Dummy


def test_registry_unknown_driver_raises():
    registry._REGISTRY.clear()
    with pytest.raises(ValueError, match="unknown driver"):
        registry.lookup("nope")


def test_registry_duplicate_name_raises():
    registry._REGISTRY.clear()

    @registry.register("dup")
    class A:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @registry.register("dup")
        class B:
            pass
