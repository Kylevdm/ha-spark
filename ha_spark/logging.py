"""Logging setup using the standard library."""

from __future__ import annotations

import logging

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, idempotently."""
    global _CONFIGURED
    # httpcore's DEBUG trace logs response status lines and headers, which can
    # carry a Supervisor's or supplier's reason phrase; it never logs above DEBUG.
    logging.getLogger("httpcore").setLevel(logging.INFO)
    # websockets' DEBUG trace logs every frame, including HA's auth message
    # with its access token and raw HA error objects.
    logging.getLogger("websockets").setLevel(logging.INFO)
    if _CONFIGURED:
        logging.getLogger().setLevel(level.upper())
        return
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)
