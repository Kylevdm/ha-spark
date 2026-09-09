# Phase 10 — R48 rectifier control: banked findings

*Date: 2026-06-28. Status: BANKED (not started). Seed for a future brainstorm.*

Discovered while building the V2L observe/tally/notify spike (PR #33): the V2L
rectifier is **controllable**, which turns V2L from observe-only into a
controllable charge source — roadmap **Phase 10** ("R48 rectifier drivers;
V2L-fed from the car; planner chooses among charge sources"). Banked here for a
proper brainstorm → spec → plan; **not** to be hacked in ad hoc.

## Topology

`car battery → car V2L inverter → AC (3 kW smart-breaker cap, 13 A @ 230 V) →
R48 rectifier → home battery DC`.

The R48 (Emerson/Vertiv **R48-3000e3**, 3 kW) is a CC/CV power supply: set output
voltage (to battery V) + max output current to dial charge power, cap AC draw via
max-input-current, gate DC output on/off.

## Controller

- ESPHome device **`esphome-emerson`**, repo
  `github.com/Kylevdm/esphome-emerson-vertiv-r48`.
- CAN bus via MCP2515 (ESP8266), talks to HA (MQTT in the example; entities are
  present in HA either way).
- Component surface (from the repo):
  - **sensors:** `output_voltage`, `output_current`, `output_temp`,
    `input_voltage` (AC), `max_output_current`.
  - **numbers (controllable):** `output_voltage` (set), `max_output_current`,
    `max_input_current`.
  - **switches:** `ac_sw`, `dc_sw` (DC output on/off), `fan_sw`, `led_sw`.
  - **buttons:** `set_offline_values`, restart.

## Live HA entities (pin these before building)

The HA estate has **several R48 instances**; only one is online. Confirm the
device at build time — names are inconsistent.

**Live / online — `Emerson_Vertiv_R48`:**
- `number.emerson_vertiv_r48_r48_set_output_voltage` = **54.0 V**
- `number.emerson_vertiv_r48_r48_max_output_current` = **50.0 A**
- `number.emerson_vertiv_r48_r48_max_input_current` = **13.0 A** (≈ 3 kW @ 230 V)
- `binary_sensor.emerson_vertiv_r48_r48_v2l_controller_status` = **on**

**Offline / spare instances** (currently `unavailable`): `r48_ac_charger`
(`number.esphome_emerson_charger_*`), `r48-v2l-charger`
(`number.r48_v2l_charger_*`), `Power_Hub` (`number.power_hub_r48_*`).

**Separate V2L smart-breaker device** (protection, distinct from the rectifier):
`binary_sensor.v2l_car_connected`, `number.v2l_over_current_threshold`,
`v2l_over_voltage_threshold`, `v2l_under_voltage_threshold`,
`v2l_power_threshold`, `v2l_temperature_threshold`, `v2l_countdown`,
`select.v2l_indicator_mode`.

**V2L discharge power:** `sensor.v2l_power` (W, device_class power) — wired into
the observe spike (PR #33).

## Limits / units

- AC input cap: **13 A @ 230 V ≈ 3 kW** (the V2L smart-breaker hard limit).
- DC output: up to **54 V × 50 A ≈ 2.7 kW**.
- Battery is ~51 V nominal (`battery_voltage_v`); 54 V is a plausible charge
  setpoint — confirm against the BMS before any write.
- **DC vs AC current:** rectifier output is DC; the 13 A cap is AC. Convert by
  the voltage ratio before comparing to grid/fuse limits (see the standing
  battery-DC-vs-AC note). Do not mix DC amps with AC amps.

## Safety architecture required (non-negotiable, per CLAUDE.md)

This is real actuation into a 3 kW rectifier charging the home battery. Any
control path MUST:

- Keep the **planner the sole decider** (the LLM never reaches `call_service`).
- Live in the `devices/` **driver layer** (Phase 7 foundation) as an R48 driver
  with `Capability` + `ControlAuthority`.
- Gate every real write on **`control == ha_spark` AND `PROACTIVE_MODE == on`**.
- Never write on an **invalid SoC**; always **read-back verify**; isolate
  failures per write.
- Respect both the AC input cap and DC output limits; validate setpoints against
  the battery's safe charge voltage/current.

## Dependencies / sequence

- Depends on **Phase 7** device-driver core (partly done on
  `worktree-feat-agent-surface`: `devices/base.py`, `registry.py`, config shim).
- Pairs with **Phase 8** tariff schedule (when is car-charging worth the
  round-trip loss?) and **Phase 10** charge-source selection in the planner
  (grid vs solar vs V2L-R48 by cost/availability).

## Next step when resumed

Brainstorm an R48 control driver + planner charge-source selection: which device
instance, capability mapping, setpoint math (target V / max I from desired
charge power, respecting AC+DC limits), authority/PROACTIVE_MODE gating, and the
round-trip-economics decision (reuse the V2L efficiency knob).
