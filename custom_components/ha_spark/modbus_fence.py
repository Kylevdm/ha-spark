"""Synchronous write-admission fence for HA's loaded Modbus endpoints.

HA internals are inspected only when already loaded; never import Modbus or
perform I/O here. Unreadable registries fail closed, with fixed repair codes.
The installed-HA source tripwire pins the internal surfaces we depend on.
"""
import sys
from typing import Any, Literal, TypeGuard

from homeassistant.core import HomeAssistant


def hub_conflict(
    hass: HomeAssistant, host: str | None, port: int,
) -> Literal["modbus_hub_conflict", "modbus_hub_unreadable"] | None:
    """Match host as written (case-insensitive), without DNS or config parsing."""
    if "modbus" not in hass.config.components:
        return None
    unreadable = False
    endpoints: list[object] = []
    try:
        const = sys.modules.get("homeassistant.components.modbus.const")
        connection = sys.modules.get("homeassistant.components.modbus.connection")
        key = getattr(const, "DATA_MODBUS_HUBS", None)
        info = getattr(connection, "async_get_connection_info", None)
        if key is None or not callable(info):
            return "modbus_hub_unreadable"
        # An absent registry means no YAML hub; a present one must be a dict.
        if key in hass.data:
            hubs = hass.data[key]
            if not isinstance(hubs, dict):
                unreadable = True
            else:
                for hub in hubs.values():
                    try:
                        endpoints.append(getattr(hub, "endpoint", None))
                    except Exception:
                        unreadable = True
        try:
            connections = info(hass)
            if not isinstance(connections, list):
                unreadable = True
            else:
                for item in connections:
                    try:
                        endpoints.append(getattr(item, "endpoint", None))
                    except Exception:
                        unreadable = True
        except Exception:
            unreadable = True
        hostname = host.lower() if host is not None else None
        serial_host = f"[{hostname}]" if hostname and ":" in hostname else hostname
        for endpoint in endpoints:
            if not _well_formed(endpoint):
                unreadable = True
                continue
            if hostname is not None and (
                len(endpoint) == 3 and endpoint[1].lower() == hostname and endpoint[2] == port
                or len(endpoint) == 2 and endpoint[0] == "serial"
                and endpoint[1].lower() == f"socket://{serial_host}:{port}"
            ):
                return "modbus_hub_conflict"
    except Exception:
        return "modbus_hub_unreadable"
    return "modbus_hub_unreadable" if unreadable else None


def _well_formed(endpoint: object) -> TypeGuard[tuple[Any, ...]]:
    """A network endpoint, or a serial device; a socket URL must carry host:port."""
    if not isinstance(endpoint, tuple) or not all(isinstance(p, str) for p in endpoint[:2]):
        return False
    if len(endpoint) == 3:
        port = endpoint[2]
        return bool(endpoint[1]) and type(port) is int and 0 < port < 65536
    if len(endpoint) != 2 or endpoint[0] != "serial" or not endpoint[1]:
        return False
    device = endpoint[1].lower()
    if not device.startswith("socket://"):
        return True
    host, sep, port_text = device[len("socket://"):].rpartition(":")
    return bool(sep and host and port_text.isdigit() and 0 < int(port_text) < 65536)
