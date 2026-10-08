# Research: AlphaESS local Modbus — models, register map, and charge/discharge control

Issue: [#250](https://github.com/Kylevdm/ha-spark/issues/250), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).
Date: 2026-10-08.

## Question

Which AlphaESS models expose local Modbus (RTU/TCP), through which interface,
and with what register map? Which open-source projects implement it? What
control surface exists for timed charge, SoC targets, discharge stop or hold
(relevant to [#196](https://github.com/Kylevdm/ha-spark/issues/196)), and
dispatch? How does it compare to the cloud API the current driver
(`ha_spark/devices/inverters/alphaess.py`) uses?

This builds on `docs/research/inverter-drivers.md` §4, which covers the cloud
Open API and concluded that local Modbus was "real but community
reverse-engineered … not a documented vendor contract". **That conclusion
needs correcting.** AlphaESS has published its own Modbus documents (see
Sources), so the register map has a vendor source, even if the vendor does not
version or support it well.

## Verdict

- **Local Modbus is a vendor-documented surface across the SMILE family.**
  RTU over RS485 works on all of the documented models. Modbus TCP on port 502
  works on the LAN-equipped models. All use one register map, with slave/unit
  ID `0x55` (85).
- **It is a strict superset of the cloud surface for ha-spark's needs.** It
  has the same two charge windows and two discharge windows with SoC cut-offs,
  and adds a **dispatch block** (`0x0880–0x0888`). The dispatch block takes a
  signed power setpoint in watts, a mode, a stop SoC and a duration that acts
  as a dead-man timer. Every one of these registers is R/W, so read-back is
  possible.
- **#196 has a local answer:** dispatch mode 1 ("battery only charges from PV",
  where the battery is not allowed to discharge), or mode 2/3 with active power
  `32000` (0 W), holds the battery. The duration register means a hold expires
  on its own if ha-spark dies, which is a safe failure. The behaviour still has
  to be tested on hardware before ha-spark relies on it (see Open questions).
- **Two existing defects or ambiguities surfaced:** the dispatch SoC scale
  (0.4 %/bit in the vendor document, 0.392 in two community projects), and the
  cloud service field casing that the current driver sends (`chargeStopSOC` vs
  `chargestopsoc`).

## 1. Models and physical interface

Source: AlphaESS Europe, *Handbuch Modbus RTU/TCP – STORION SMILE V2.1*
(§1–3; mirrored in
[ramonvanraaij/ha-alphaess-modbus `docs/alphaess_modbus_rtu_tcp.pdf`](https://github.com/ramonvanraaij/ha-alphaess-modbus/blob/fdac291/docs/alphaess_modbus_rtu_tcp.pdf);
original at [alphaess.de](https://alphaess.de/Public/Uploads/uploadfile/files/20230926/AlphaESS-Handbuch_SMILE_ModBus_RTU_TCP_V21.pdf)).

| Model | RTU (RS485, 9600 baud) | TCP (LAN, port 502) |
|---|---|---|
| SMILE5 (HW v1 and v2) | CAN/RS485 port, pins 4 = B, 5 = A | LAN port |
| SMILE-i3 | CAN/RS485, 4B/5A | not listed |
| SMILE-T10 | **"Dispatch" port, pins 3 = B, 6 = A** | not listed |
| SMILE-Hi5 / Hi10 | CAN/RS485, 4B/5A | LAN port |
| SMILE-G3-S3.6 / B5 / S5 | CAN/RS485, 4B/5A | LAN port |
| SMILE-BAT-8.2PHA | RS485, 4B/5A | LAN port |

Protocol facts from the same document:

- **RTU:** 8N1, half-duplex. Response under 300 ms, command interval over
  300 ms, timeout over 10 s.
- **TCP:** the EMS is the server on port 502. Response under 100 ms, command
  interval over 100 ms.
- **Function codes:** `0x03` read holding registers, `0x10` write multiple
  registers, `0x06` write single register. The default address is `0x55`, in
  the range 1–247.

Beyond the 2022 document:

- ramonvanraaij reports SMILE-G3-T10 working over TCP. Contributors report
  SMILE5, G3-S5, Hi10, B3 and B3-PLUS working
  ([README "Compatibility"](https://github.com/ramonvanraaij/ha-alphaess-modbus/blob/fdac291/README.md)).
- senalse targets SMILE-M5, SMILE5, G3, Hi and B3, and says Modbus TCP must be
  enabled in the AlphaESS app under *Settings → Communication*
  ([README](https://github.com/senalse/ha-alphaess-modbus/blob/3b944d6/README.md)).
- Alpha2MQTT lists SMILE-B3 RTU wiring on the CAN/RS485 port (pin 4 = B−,
  pin 5 = A+)
  ([README](https://github.com/dxoverdy/Alpha2MQTT/blob/febfd78/README.md)).
- Model-specific gaps: `0x072C–0x072F` (user/battery mode) applies to HHE MEC
  models only, and G3-T10 does not expose EV-charger data locally
  (ramonvanraaij README).

**Implication for #247:** whether TCP is available is a per-model question.
The integration's config flow should offer TCP first and RTU through a
user-supplied RS485 gateway second. It should not assume either.

## 2. Register map: the parts ha-spark needs

Primary source: the AlphaESS *Register Parameter List*
([alphaess.de PDF](https://alphaess.de/Public/Uploads/uploadfile/files/20230926/AlphaESS_Register_Parameter_List.pdf),
mirrored as
[`docs/alphaess_modbus_register_parameter_list.pdf`](https://github.com/ramonvanraaij/ha-alphaess-modbus/blob/fdac291/docs/alphaess_modbus_register_parameter_list.pdf)).
It was cross-checked against two independent implementations: senalse's
`const.py` and `docs/register_map.md`, and Alpha2MQTT's `Definitions.h`.

### Telemetry (read-only)

| Addr | Meaning | Type / scale |
|---|---|---|
| `0x0021` | Grid power, total (+ import, − export) | int32, W |
| `0x0102` | Battery SoC | int16, ×0.1 % |
| `0x0100` / `0x0101` | Battery voltage / current | ×0.1 V / ×0.1 A |
| `0x0126` | Battery power (sign convention differs between sources; verify) | int16, W |
| `0x0111` / `0x0112` | BMS max charge / discharge current now | ×0.1 A |
| `0x0120` / `0x0122` / `0x0124` | Lifetime battery charge / discharge / charge-from-grid | uint32, ×0.1 kWh |
| `0x0010` / `0x0012` | Lifetime grid export / import (meter) | uint32, ×0.01 kWh |
| `0x08D4` | System fault bitmap | uint32 |

### Timed charge/discharge ("Household time period control")

| Addr | Vendor name | Notes |
|---|---|---|
| `0x084F` | Time period control flag | 0 disable, 1 enable charge, 2 enable discharge, 3 both |
| `0x0850` | UPS Reserve SoC | community projects treat this as the discharge cut-off SoC |
| `0x0851–0x0854` | Discharge start/stop times 1–2, **hours** | |
| `0x0855` | Charge Cut SoC | the target-SoC stop for grid charge |
| `0x0856–0x0859` | Charge start/stop times 1–2, **hours** | |
| `0x085A–0x085D` | Discharge start/stop times 1–2, **minutes** | |
| `0x085E–0x0861` | Charge start/stop times 1–2, **minutes** | |

Hours and minutes sit in separate, non-adjacent registers, so one window takes
four single-register writes, or one block write over `0x0850–0x0861`.

This is the **same model as the cloud `setbatterycharge` /
`setbatterydischarge` surface**: two windows, a cut-off SoC and an enable
flag. It is not a power setpoint.

### Dispatch block (power control)

Source: AlphaESS *Modbus/Server API Guide V1.0 EN* (2022-11-11, marked "not
released"), §1–2
([mirror](https://github.com/ramonvanraaij/ha-alphaess-modbus/blob/fdac291/docs/alphaess_modbus_dispatch_function.pdf)).

| Addr | Para | Meaning | Encoding |
|---|---|---|---|
| `0x0880` | 1 | Dispatch start | 1 start, 0 stop |
| `0x0881–0x0882` | 2 | Active power | int32, 1 W/bit, **offset 32000**: < 32000 charge, > 32000 discharge |
| `0x0883–0x0884` | 3 | Reactive power | same offset; set to 32000 / 0 |
| `0x0885` | 4 | Dispatch mode | see below |
| `0x0886` | 5 | Stop SoC | **0.4 %/bit** (vendor: 250 = 100 %, 95 = 38 %) |
| `0x0887–0x0888` | 6 | Duration | uint32, seconds |
| `0x0889` / `0x088A` | 7 / 8 | Flow direction / PV switch | community: 255; PV 0 unchanged, 1 on, 2 off |

The vendor document says that "all registers should receive correct command"
for a dispatch. Its worked example is one `0x10` write of 9 registers from
`0x0880`: a 2 kW charge in mode 2 to 100 % SoC for 500 s. senalse and
ramonvanraaij write 11 registers, which adds `0x0889`/`0x088A`.

Dispatch modes (vendor Table 1):

| Mode | Name | Vendor description |
|---|---|---|
| 1 | Battery only charges from PV | "the battery is **not allowed to discharge**"; PV surplus charges it, the rest is exported |
| 2 | State of Charge control | force charge or discharge at P until the stop SoC |
| 3 | Load following | self-consumption, with P as a limit |
| 4 | Maximise output | battery discharges if PV cannot meet the AC output |
| 5 | Normal | self-consumption |
| 6 | Optimise consumption | PV first, grid tops up battery charge |
| 7 | Maximise consumption | battery charges from grid only |
| 19 | No battery charge (EMS-version-specific) | self-consumption, charge capped at P |

ramonvanraaij also documents modes 21–25 (OSW, grid-priority variants) without
a vendor citation. Treat them as unverified.

**Dead-man behaviour:** when the duration expires, the inverter returns to its
default (self-consumption) operation. Both HA projects build on this. They
re-issue the dispatch with short durations and write a full reset (start = 0,
duration 90 s) on startup, on lost connection or when a timer expires
(ramonvanraaij README "Dead Man's Switch"; senalse `coordinator.py`
`async_reset_dispatch`).

## 3. Open-source implementations

| Project | Transport | Control | Notes |
|---|---|---|---|
| [senalse/ha-alphaess-modbus](https://github.com/senalse/ha-alphaess-modbus) | TCP (pymodbus ≥ 3.7.4), HACS custom component | dispatch force charge/discharge/export/import, time windows, cut-off SoCs, max feed-in (`0x0800`), clock sync (`0x0740`, BCD) | Closest to the shape #247 wants: a Python `custom_components` integration with a register-def table. MIT. Ported from Axel Koegler's YAML package. |
| [ramonvanraaij/ha-alphaess-modbus](https://github.com/ramonvanraaij/ha-alphaess-modbus) | TCP, HA YAML package (core `modbus:` + helpers) | same force modes, all through dispatch mode 2 with computed P | Bundles the vendor PDFs. BSD-3. |
| [dxoverdy/Alpha2MQTT](https://github.com/dxoverdy/Alpha2MQTT) | RTU, ESP8266/ESP32 to MQTT | full register read/write, dispatch | Follows the vendor's v1.23 RTU document. Uses dispatch SoC 0.4 %/bit. |
| [SorX14/alphaess_modbus](https://pypi.org/project/alphaess-modbus) | RTU (and TCP), async Python library | read-focused | Register map generated from the vendor PDF as JSON. |
| [@impact0815/node-red-contrib-alphaess-modbus](https://flows.nodered.org/node/@impact0815/node-red-contrib-alphaess-modbus) | TCP, Node-RED | optional dispatch | Cites the "Household Modbus Register Parameter List". |

**ESPHome:** there is no maintained AlphaESS ESPHome component. ESPHome's
generic `modbus_controller` over RS485 would work with the map above.
Alpha2MQTT is the closest ESP-native project.

## 4. Control surface against ha-spark's needs

| Need | Cloud (current driver) | Local Modbus |
|---|---|---|
| Timed charge to target SoC | `setbatterycharge`: 2 windows + `chargestopsoc` (+ `cp1power`/`cp2power` in current HA services.yaml) | `0x084F` + `0x0855–0x0861`, 2 windows |
| Discharge window / floor | `setbatterydischarge`: 2 windows + `dischargecutoffsoc` | `0x084F` + `0x0850–0x085D` |
| Live charge rate (W) | no, apart from the newer per-window `cpNpower`, not verified | **yes**: dispatch P, 1 W resolution |
| **Discharge hold (#196)** | indirect only: discharge-time-control enable with no window covering now (unverified semantics) | **dispatch mode 1**, or mode 2/3 with P = 32000, with a duration timer |
| Force discharge / export | no | dispatch mode 2, P > 32000, stop SoC |
| Read-back | GET config endpoints, minutes of cloud latency | every control register is R/W, readable within one poll |
| Fail-safe if ha-spark dies | the schedule persists indefinitely | dispatch expires after its duration and falls back to self-consumption |
| Latency / throttle | cloud, community guidance ≥ 10 s/call (inverter-drivers.md §4) | TCP command interval > 100 ms, RTU > 300 ms |
| Credentials | AppID/AppSecret to the cloud | none on the wire. **Modbus has no authentication**, so anyone on the LAN can write. |

Sources for the cloud column: HA integration
[`services.yaml`](https://github.com/CharlesGillanders/homeassistant-alphaESS/blob/main/custom_components/alphaess/services.yaml)
and the wrapper
[`alphaess/alphaess.py`](https://github.com/CharlesGillanders/alphaess-openAPI/blob/768a71f/alphaess/alphaess.py)
(`setbatterycharge`, `setbatterydischarge`, `update(Dis)ChargeConfigInfo`,
`setTimeChargeBySn`). The vendor dispatch guide (§2.1) shows that **the cloud
server has the same 8-parameter dispatch command**. The public Open API wrapper
does not expose it, so for third parties dispatch is local-only.

### Fit with the driver contract

- `Capability.CHARGE_WINDOW` maps to the time-period registers, the same
  primitive the driver has today.
- Dispatch adds a **live power setpoint primitive with a mandatory renewal
  timer**. This is the same family as Victron's 60 s heartbeat
  (inverter-drivers.md §5–6), not Solis's current setpoint. The capability
  model needs "setpoint + TTL".
- A discharge hold fits `reconcile_holds` / `write_safe_state`. The hold is a
  dispatch with a bounded duration that ha-spark renews every cycle, and the
  safe state is a dispatch reset.

## 5. Findings that affect existing code (flagged, not changed)

1. **Cloud service field casing.** The current HA integration `services.yaml`
   declares `chargestopsoc` (lowercase). `alphaess.py:80` sends
   `"chargeStopSOC"`, which confirms the open "VERIFY before shipping" comment
   at `alphaess.py:68`. HA service schemas usually reject or ignore unknown
   keys, so the stop-SoC may not be applied today. This needs its own ticket
   and a check on the reference box.
2. **Dispatch SoC scale.** The vendor documents and Alpha2MQTT give
   0.4 %/bit (100 % = 250). senalse and ramonvanraaij use 0.392 (100 % ≈ 255).
   Any implementation should follow the vendor and read back.

## Open questions (need the reference household's hardware)

- **Model and firmware:** does the reference unit's model and EMS firmware
  expose TCP? Is TCP enabled? Does a read of `0x0880–0x088A` succeed?
- **Mode 1 vs P = 32000:** does mode 1 actually block discharge under house
  load and EV load on this firmware? The vendor text says yes, but nobody has
  published a test. Mode 1 is the clean hold. P = 32000 in mode 3 is what
  ramonvanraaij documents as "freeze".
- **Cloud vs local precedence:** if AlphaCloud or the app writes a schedule
  while a local dispatch is active, which wins? And do local writes to
  `0x0850–0x0861` show up in the app? No source covers either.
- **Flash endurance:** is anything known about the endurance of the
  time-period registers, as #109 asked for Solis? No source says. Dispatch
  appears designed for frequent rewrites, since it is the vendor's own
  aggregator interface, but this is unconfirmed.
- **Concurrent Modbus TCP clients:** the EMS's limit on concurrent TCP clients
  is undocumented. This matters if the HACS integration and any existing
  Modbus integration run side by side during migration.

## Sources

- AlphaESS, *Modbus/Server API Guide V1.0 EN* (dispatch), 2022-11-11, mirrored
  in ramonvanraaij/ha-alphaess-modbus `docs/alphaess_modbus_dispatch_function.pdf` @ `fdac291`.
- AlphaESS Europe, *Handbuch Modbus RTU/TCP – STORION SMILE V2.1*, alphaess.de (2023-09-26 upload), mirrored as `docs/alphaess_modbus_rtu_tcp.pdf`.
- AlphaESS, *Register Parameter List*, alphaess.de, mirrored as `docs/alphaess_modbus_register_parameter_list.pdf`.
- senalse/ha-alphaess-modbus @ `3b944d6`: `README.md`, `docs/register_map.md`, `custom_components/alphaess_modbus/{const,coordinator,switch}.py`.
- ramonvanraaij/ha-alphaess-modbus @ `fdac291`: `README.md`.
- dxoverdy/Alpha2MQTT @ `febfd78`: `README.md`, `Alpha2MQTT/Definitions.h`.
- CharlesGillanders/homeassistant-alphaESS `services.yaml` (main, fetched 2026-10-08); CharlesGillanders/alphaess-openAPI @ `768a71f`.
- Prior in-repo work: `docs/research/inverter-drivers.md` §4–6; issues #196, #71, #109.
