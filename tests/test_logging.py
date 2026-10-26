"""Library DEBUG traces that can carry credentials stay off at DEBUG."""

import logging

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
