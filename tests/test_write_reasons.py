"""Fixed native refusal codes agree with the integration and notice allowlist."""
import json
from pathlib import Path
from typing import get_args

from ha_spark.api.commands import CommandResult, IntentResult, WriteReason
from ha_spark.energy.native_export_notices import _REASONS


def test_write_reason_contract() -> None:
    fixture = Path(__file__).parent / "fixtures/contract/write_reasons.json"
    reasons = json.loads(fixture.read_text())
    assert sorted(get_args(WriteReason)) == reasons
    assert set(reasons) <= _REASONS
    for reason in reasons:
        assert IntentResult(outcome="refused", reason=reason).reason == reason
        assert CommandResult(session_id="test", sequence=1, outcome="refused",
                             reason=reason).reason == reason
