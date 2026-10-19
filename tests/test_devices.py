import pytest

from ha_spark.devices import registry
from ha_spark.devices.base import Capability
from ha_spark.devices.execution import Ownership, execution_context
from tests.authority_helpers import legacy_settings


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


@pytest.mark.parametrize("mode,expected", [
    ("optimise", "on"), ("observe", "simulate"), ("observe", "simulate"),
])
def test_explicit_site_mode_gates_writes(mode, expected):
    settings = legacy_settings(authority_mode=mode)
    assert execution_context().gate.mode_for(
        Ownership.CHARGING, settings.devices[0], settings
    ) == expected


@pytest.mark.parametrize("legacy_control", ["observe", "supplier"])
@pytest.mark.parametrize("legacy_mode", ["on", "simulate", "off"])
def test_retired_device_control_never_grants_writes(legacy_control, legacy_mode):
    settings = legacy_settings(authority_mode="observe", proactive_mode=legacy_mode)
    config = settings.devices[0].model_copy(update={"control": legacy_control})
    assert execution_context().gate.mode_for(Ownership.CHARGING, config, settings) == "simulate"


def test_capability_values():
    assert Capability.CHARGE_RATE == "charge_rate"


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
