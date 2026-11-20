"""Dated charge intents are validated and refused as not available until Slot 2 arms (#699)."""
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.ha_spark.commands import (
    Command,
    DatedChargeNone,
    DatedChargeUntrusted,
    DatedChargeWindow,
)
from custom_components.ha_spark.coordinator import SparkCoordinator
from tests_ha.fake_solis import FakeSolisDevice
from tests_ha.test_native_write import command, hass_config_dir, hass_storage, writer

__all__ = ["hass_config_dir", "hass_storage", "writer"]

FIXTURE = (Path(__file__).parents[1]
           / "tests/fixtures/contract/command_apply_dated_charge_intents.json")
WINDOW = {"kind": "dated_charge", "request": "window", "current_a": 20.0,
          "start": "2026-10-24T12:00:00+01:00", "end": "2026-10-24T13:00:00+01:00"}


def test_integration_schema_accepts_the_add_on_golden() -> None:
    body = json.loads(FIXTURE.read_text())
    Command.model_validate(body)
    models: tuple[type[DatedChargeNone | DatedChargeUntrusted | DatedChargeWindow], ...] = (
        DatedChargeNone, DatedChargeUntrusted, DatedChargeWindow)
    for model, raw in zip(models, body["payload"]["intents"], strict=True):
        assert model.model_validate(raw).model_dump(mode="json") == raw


def apply(writer: SparkCoordinator, *intents: dict[str, Any]) -> Command:
    base = command(writer)
    inverter_id = base.payload["intents"][0]["inverter_id"]  # type: ignore[index,call-overload]
    return base.model_copy(update={"payload": {"intents": [
        base.payload["intents"][0],  # type: ignore[index]
        *({"inverter_id": inverter_id, **intent} for intent in intents),
    ]}})


@pytest.mark.parametrize("intent", [
    {"kind": "dated_charge", "request": "none"},
    {"kind": "dated_charge", "request": "untrusted"},
    WINDOW,
])
async def test_a_valid_dated_charge_is_not_available_and_writes_nothing(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, intent: dict[str, Any],
) -> None:
    result = await writer._async_dispatch_command(apply(writer, intent))
    assert result.intents[0].outcome == "applied"
    assert (result.intents[1].outcome, result.intents[1].reason) == ("refused", "not_available")
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")


@pytest.mark.parametrize("change", [
    {"start": "2026-10-24T12:00:00", "end": "2026-10-24T13:00:00"},
    {"start": "2026-10-24T13:00:00Z", "end": "2026-10-24T12:00:00Z"},
    {"current_a": 99.5},
    {"current_a": -1.0},
    {"request": "later"},
    {"surprise": "secret-value"},
])
async def test_a_malformed_dated_charge_is_refused_without_echo(
    writer: SparkCoordinator, native_transport: FakeSolisDevice, change: dict[str, Any],
) -> None:
    result = await writer._async_dispatch_command(apply(writer, {**WINDOW, **change}))
    assert result.intents[0].outcome == "applied"
    assert (result.intents[1].outcome, result.intents[1].reason) == ("refused", "invalid_intent")
    text = result.model_dump_json()
    assert "secret-value" not in text and "99.5" not in text and "2026" not in text
    assert all(t.address == 43141 for t in native_transport.traffic if t.op == "write")
