#!/usr/bin/env python3
"""Timestamped recorder for the Solis forced-charge live-fire run (#83).

Polls the instrumentation entities from docs/runbooks/solis-forced-charge-live-fire.md
and prints one row per change, so the settling delay in step 2 is measured rather than
guessed. Read-only: it never writes to Home Assistant.

Usage:
    python3 docs/runbooks/live_fire_recorder.py [--interval 2] [--csv run.csv]

Reads HA_URL and HA_TOKEN from the environment or from .env in the repo root.
Stop with Ctrl-C; a summary of state transitions is printed on exit.
"""

from __future__ import annotations

import argparse
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
    "axle": "sensor.axle_vpp_axle_event_window_state",
}

# Columns that must hold steady; a change is called out loudly.
GUARDS = {"mode": "35", "health": "Healthy", "pwr_sw": "On"}


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
        sys.exit("HA_URL and HA_TOKEN must be set (environment or .env)")
    return url.rstrip("/"), token


def fetch(url: str, token: str) -> dict[str, str]:
    req = urllib.request.Request(
        f"{url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        states = {s["entity_id"]: s["state"] for s in json.load(resp)}
    return {key: states.get(eid, "MISSING") for key, eid in ENTITIES.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    ap.add_argument("--csv", help="also append rows to this CSV file")
    args = ap.parse_args()

    url, token = load_env()
    cols = list(ENTITIES)
    writer = None
    if args.csv:
        fh = open(args.csv, "a", newline="")
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(["time", *cols])

    header = f"{'time':<12} " + " ".join(f"{c:>8}" for c in cols)
    print(header)
    print("-" * len(header))

    previous: dict[str, str] | None = None
    transitions: list[str] = []
    started = time.monotonic()

    try:
        while True:
            try:
                row = fetch(url, token)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                print(f"{datetime.now():%H:%M:%S}    poll failed: {exc}")
                time.sleep(args.interval)
                continue

            now = datetime.now()
            if row != previous:
                stamp = f"{now:%H:%M:%S}"
                print(f"{stamp:<12} " + " ".join(f"{row[c]:>8}" for c in cols))
                if writer:
                    writer.writerow([now.isoformat(timespec="seconds"), *(row[c] for c in cols)])
                    fh.flush()

                if previous is not None:
                    for key, expected in GUARDS.items():
                        if row[key] != previous[key] and row[key] != expected:
                            print(f"  ** ABORT CONDITION: {key} = {row[key]} (expected {expected})")
                    if row["rc_on"] != previous["rc_on"]:
                        elapsed = time.monotonic() - started
                        note = (
                            f"{stamp}  rc_force_charge "
                            f"{previous['rc_on']} -> {row['rc_on']}  (+{elapsed:.1f}s)"
                        )
                        transitions.append(note)
                        print(f"  ** {note}")
                previous = row

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n\nRC enable transitions:")
        for t in transitions or ["  (none observed)"]:
            print(" ", t)


if __name__ == "__main__":
    main()
