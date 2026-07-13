"""Deprecated: inverter drivers moved to ``ha_spark/devices/inverters/`` (Phase
7). Kept as a slim re-export for any stray importer; removed next release.

Use ``ha_spark.devices.get_device``/``inverter_device`` instead of the old
``charger_for``.
"""
from __future__ import annotations

from ha_spark.devices.inverters.alphaess import AlphaESSDevice
from ha_spark.devices.inverters.solis import SolisDevice, solis_current_a  # noqa: F401

SolisCharger = SolisDevice  # back-compat alias; removed when chargers.py is deleted
AlphaESSCharger = AlphaESSDevice  # back-compat alias; removed when chargers.py is deleted
