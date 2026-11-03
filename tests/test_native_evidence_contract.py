"""Native battery-direction evidence does not invent an HA entity id."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ha_spark.api.snapshot_v4 import Evidence, EvidenceSource


def test_native_direction_contract() -> None:
    evidence = Evidence.model_validate(json.loads((
        Path(__file__).parent / "fixtures/contract/evidence_native.json").read_text()))
    assert evidence.battery_direction.entity_id is None
    assert evidence.ev_status.source == "entity"


@pytest.mark.parametrize("source", [
    {"source": "entity", "max_age_s": 300},
    {"source": "native", "entity_id": "sensor.battery", "max_age_s": 300},
])
def test_invalid_direction_source_rejected(source: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceSource.model_validate(source)
