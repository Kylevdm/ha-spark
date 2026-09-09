#!/usr/bin/env python3
"""Read-only wide poller for the Axle export event (#83 observation).

Companion to live_fire_recorder.py, which keeps the narrower live-fire column set.
This one adds the timed-slot control surface and the cloud-control integration, to
identify WHICH surface a third party actually uses to command this inverter.

Read-only: never writes to Home Assistant.
"""
from __future__ import annotations

import csv
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

ENTITIES = {
    # what we already watch
    "rc_on": "switch.solis_control_rc_force_charge",
    "rc_w": "sensor.solis_control_rc_force_charge_power",
    "rc_to": "sensor.solis_control_rc_timeout",
    "mode": "sensor.solis_control_work_mode_bitfield",
    "batt_w": "sensor.solisac_battery_power",
    "batt_a": "sensor.solisac_battery_current",
    "soc": "sensor.solisac_battery_soc",
    "grid_w": "sensor.solisac_meter_active_power",
    "health": "sensor.solisac_communication_health",
    "pwr_sw": "select.solisac_power_switch",
    # axle
    "axle_st": "sensor.axle_vpp_axle_event_window_state",
    "axle_ip": "sensor.axle_vpp_axle_event_in_progress",
    "axle_dir": "sensor.axle_vpp_axle_import_export",
    # timed-discharge slot surface
    "td_cur": "number.solisac_timed_discharge_current",
    "td_sh": "number.solisac_timed_discharge_start_hours",
    "td_sm": "number.solisac_timed_discharge_start_minutes",
    "td_eh": "number.solisac_timed_discharge_end_hours",
    "td_em": "number.solisac_timed_discharge_end_minutes",
    # timed-charge slot surface (should stay 23:30-05:30 @ 60)
    "tc_cur": "number.solisac_timed_charge_current",
    "tc_sh": "number.solisac_timed_charge_start_hours",
    # rate limits
    "dis_cur": "number.solisac_battery_discharge_current",
    "cd_cur": "number.solisac_battery_chargedischarge_current",
}

# Solis *cloud* integration (~5 min delay — never derive latency or ordering from these;
# they are a binary "did this surface move at all" signal only). Entity ids embed the
# inverter serial, so they are discovered by suffix rather than hard-coded.
CLOUD_SUFFIXES = {
    "cc_slot1_dis": "_slot1_discharge_current",
    "cc_slot1_time": "_slot1_discharge_time",
    "cc_reserve": "_battery_reserve",
}


def discover_cloud(states: dict[str, str]) -> dict[str, str]:
    found = {}
    for key, suffix in CLOUD_SUFFIXES.items():
        for eid in states:
            if "inverter_control_" in eid and eid.endswith(suffix):
                found[key] = eid
                break
    return found
GUARDS = {"mode": "35", "health": "Healthy"}


def load_env() -> tuple[str, str]:
    url, token = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    if not (url and token):
        env = pathlib.Path(__file__).resolve().parents[2] / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip("'\"")
                if k.strip() == "HA_URL" and not url:
                    url = v
                elif k.strip() == "HA_TOKEN" and not token:
                    token = v
    if not (url and token):
        sys.exit("HA_URL and HA_TOKEN must be set")
    return url.rstrip("/"), token


def main() -> None:
    url, token = load_env()
    cols = list(ENTITIES) + list(CLOUD_SUFFIXES)
    fh = open(sys.argv[1] if len(sys.argv) > 1 else "axle.csv", "a", newline="")
    w = csv.writer(fh)
    if fh.tell() == 0:
        w.writerow(["time", *cols])
    prev: dict[str, str] | None = None
    while True:
        try:
            req = urllib.request.Request(
                f"{url}/api/states", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                states = {s["entity_id"]: s["state"] for s in json.load(r)}
            cloud = discover_cloud(states)
            row = {k: states.get(e, "MISSING") for k, e in ENTITIES.items()}
            row.update({k: states.get(e, "MISSING") for k, e in cloud.items()})
            for k in CLOUD_SUFFIXES:
                row.setdefault(k, "NOT_FOUND")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            time.sleep(3)
            continue
        if row != prev:
            w.writerow([datetime.now().isoformat(timespec="seconds"), *(row[c] for c in cols)])
            fh.flush()
            prev = row
        time.sleep(3)


if __name__ == "__main__":
    main()
