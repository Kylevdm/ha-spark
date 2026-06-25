from ha_spark.devices.base import Capability, ControlAuthority, effective_mode


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


def test_capability_and_authority_values():
    assert Capability.CHARGE_RATE == "charge_rate"
    assert set(ControlAuthority) == {
        ControlAuthority.OBSERVE,
        ControlAuthority.HA_SPARK,
        ControlAuthority.SUPPLIER,
    }
