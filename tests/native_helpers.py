"""Small validated native-site input for add-on command and SoC tests."""
from datetime import UTC, datetime

from ha_spark.api.contract import IntegrationLink, SiteSnapshot
from ha_spark.api.snapshot_v4 import BankSoc, SnapshotV4, Soc
from ha_spark.config import Settings
from ha_spark.energy.topology import Topology
from ha_spark.energy.topology_record import LoadedTopology

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def native_link(settings: Settings) -> IntegrationLink:
    topology = Topology.model_validate({
        "connections": [{"id": "connection", "supply_voltage_v": 230.0,
                         "import_limit_a": 100.0, "export_limit_a": 100.0,
                         "dno_export_limit_kw": 3.68}],
        "banks": [{"id": "bank", "connection_id": "connection",
                   "usable_capacity_kwh": settings.battery_capacity_kwh,
                   "nominal_voltage_v": settings.battery_voltage_v,
                   "min_soc": 10.0, "target_cap": 100.0,
                   "charge_limit_a": 62.5, "discharge_limit_a": 62.5,
                   "served_load_share_pct": 100.0}],
        "inverters": [{"id": "inverter", "bank_id": "bank", "driver_kind": "solis",
                       "charge_limit_a": 62.5, "discharge_limit_a": 62.5,
                       "confirmed": True, "transport": "native", "gateway_host": "gateway.test"}],
    })
    snapshot = SiteSnapshot.model_validate({
        "api_version": 4, "release_version": "0.30.0",
        "site_id": "12345678-1234-5678-1234-567812345678", "sent_at": NOW.isoformat(),
    })
    return IntegrationLink(
        snapshot, NOW, False,
        topology=LoadedTopology("native", 1, topology),
        v4=SnapshotV4(owner="integration", banks=(BankSoc(
            id=topology.banks[0].id,
            soc=Soc.model_validate({"value": 42.0, "read_at": NOW.isoformat(),
                                    "trust": "trusted", "reason": "ok"}),
        ),)),
        authority_epoch=7,
    )
