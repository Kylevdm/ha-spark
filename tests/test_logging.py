"""Library traces that can carry credentials or personal data stay off."""

import logging

import httpx
import respx

from ha_spark.logging import setup_logging


def test_debug_keeps_frame_and_wire_traces_off():
    root = logging.getLogger().level
    try:
        setup_logging("DEBUG")
        assert logging.getLogger("websockets").getEffectiveLevel() == logging.INFO
        assert not logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG)
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.INFO
    finally:
        logging.getLogger().setLevel(root)


async def test_info_logs_no_request_url(caplog):
    # httpx logs every request URL at INFO; some carry an account number or
    # home coordinates (#493).
    root = logging.getLogger().level
    try:
        setup_logging("INFO")
        # basicConfig is a no-op under pytest's handlers: set INFO explicitly.
        caplog.set_level(logging.INFO)
        with respx.mock:
            respx.get("https://api.example/v1/accounts/A-CANARY01/").respond(200, json={})
            async with httpx.AsyncClient() as client:
                await client.get("https://api.example/v1/accounts/A-CANARY01/")
        assert not [r for r in caplog.records if r.name.startswith("httpx")]
        assert "A-CANARY01" not in caplog.text
    finally:
        logging.getLogger().setLevel(root)
