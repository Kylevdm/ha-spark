# Research: reading Seplos v3 battery packs read-only

Issue: [#332](https://github.com/Kylevdm/ha-spark/issues/332), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).
Date: 2026-10-15.

## Question

What are the ways to read a Seplos v3 BMS **read-only** for a bank of two packs
in parallel (master/slave), and what does each way give the integration? The
ticket asks for six things:

- **transport** (RS485 Modbus, CAN, BLE, sniffing the inverter link, remote
  RS485 over TCP): which port each uses, and whether it is safe alongside the
  inverter's own BMS link;
- **identity**: a per-pack serial or other stable id, and how master and slave
  appear (this feeds pack devices under the bank device, #321);
- **fields**: SoC, current, voltage, cell data, and a freshness signal;
- **protocol and firmware variation** across Seplos v3 revisions;
- **fit with the v1 split**: read natively by the integration as another bus,
  or consumed from another HA integration's entities (the #276 assumption);
- whether the **JK active balancers** expose anything readable or affect the
  BMS's SoC.

The reference setup is two Seplos v3 280 Ah packs in parallel (master/slave)
with JK active balancers fitted. They feed a Solis hybrid that takes BMS data
from the master pack. Read-only throughout: ha-spark never writes BMS settings
(CONTEXT.md, *Calibration lifecycle*).

## Sources and provenance

The provenance ranking from #249 applies: **(A)** vendor documents, **(B)**
source code, **(C)** community reports, **(L)** this repo. Nothing here was
measured on the household's packs; every hardware claim is A, B or C.

- **(A) Seplos/XZH Modbus-RTU protocol, V0.1** (2023-02-09): "SEPLOS-3.0 RS485
  BMS Modbus RTU Protocol", BMS48100/48200. Byte-identical copies (md5
  `49c6dfcc…`) ship in three repos, for example
  [`Seplos3MQTT/docs/SEPLOS-3.0RS485BMS.Modbus.RTU.Protocol.pdf`](https://github.com/ferelarg/Seplos3MQTT/blob/aa14a7054bc69471bd568c40e745f39ebb9be8b1/docs/SEPLOS-3.0RS485BMS.Modbus.RTU.Protocol.pdf).
  It has no version-information (serial) block.
- **(A) XZH BMS Modbus-RTU Protocol, V0.3** (revised 2024-10-15), which Seplos
  supplied in 2026. The Chinese original, Seplos's official English edition
  (2024-11-28) and a community translation with notes from Seplos engineering
  are in syssi/esphome-seplos-bms at
  [`7595c60`](https://github.com/syssi/esphome-seplos-bms/tree/7595c6050ac443397a673a9cbcb107623469e79f/docs)
  (added in #247 and #251 of that repo, 2026-09-30 and 2026-10-12). This is
  the most complete primary source and is cited below as **V0.3**:
  [`docs/XZH BMS Modbus-RTU Protocol V0.3.md`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/docs/XZH%20BMS%20Modbus-RTU%20Protocol%20V0.3.md).
  The translation is B quoting A. Where it matters, the register tables were
  checked against the V0.1 PDF.
- **(B) syssi/esphome-seplos-bms** at `7595c60` (2026-10-12): the v3 RS485
  examples (`esp32-seplos-v3-example.yaml`, `…-multiple-battery-banks.yaml`)
  and the v3 BLE components (`components/seplos_bms_v3_ble/`,
  `components/seplos_bms_v3_ble_pack/`).
- **(B) DpunktS/seplos_v3_sniffer** at
  [`d512cb6`](https://github.com/DpunktS/seplos_v3_sniffer/tree/d512cb68be2c1f3cc6d9324eb622130b89ce859d)
  (2026-04-09): an ESPHome passive listener for parallel banks.
- **(B) ferelarg/Seplos3MQTT** at
  [`aa14a70`](https://github.com/ferelarg/Seplos3MQTT/tree/aa14a7054bc69471bd568c40e745f39ebb9be8b1)
  (2024-04-08): a Python passive listener that publishes to MQTT.
- **(B) privatecoder/seplos-mqtt-remote-rs485** at
  [`3a105df`](https://github.com/privatecoder/seplos-mqtt-remote-rs485/tree/3a105dfd299c86ec4ff8ddbfec1ec0bf8d91b87e)
  (2026-01-04): an active poller over a remote RS485-to-TCP gateway. **It
  speaks the v2/"V16" ASCII protocol, not v3** (see Transport).
- **(B) flip555/bms_connector** at
  [`6e6717f`](https://github.com/flip555/bms_connector/tree/6e6717f349b35587ccc95e105227a86daf04611d)
  (2026-05-29): an HA custom integration; its v3 path is an active Modbus
  poller marked "untested".
- **(B) patman15/aiobmsble** at
  [`2a78c0e`](https://github.com/patman15/aiobmsble/tree/2a78c0e0ad9ce018236bd39622bf77c21b20d26f)
  (2026-10-12): the BLE library behind the `bms_ble` HA integration
  ([BMS_BLE-HA `be5bb2c`](https://github.com/patman15/BMS_BLE-HA/tree/be5bb2c628f4317bea52bd5ed89a355576681be1)).
- **(B) syssi/esphome-jk-bms** at
  [`25822a8`](https://github.com/syssi/esphome-jk-bms/tree/25822a8f9ab784fa89ccdd1f3002f8e4a2e06916)
  (2026-10-14): JK and NEEY/Heltec balancer components.
- **(B/C) alienatedsec/solis-ha-modbus-cloud** at
  [`744e541`](https://github.com/alienatedsec/solis-ha-modbus-cloud/tree/744e541257da2fc19f9e9bca462b2024dd115298):
  a Solis wiring pattern that gives the battery's RS485 its own Waveshare
  gateway, separate from the inverter bus. It does not name the BMS make.
- **(C) Community threads**: syssi/esphome-seplos-bms issues
  [#80](https://github.com/syssi/esphome-seplos-bms/issues/80),
  [#112](https://github.com/syssi/esphome-seplos-bms/issues/112),
  [#241](https://github.com/syssi/esphome-seplos-bms/issues/241),
  [#242](https://github.com/syssi/esphome-seplos-bms/issues/242) and
  [#246](https://github.com/syssi/esphome-seplos-bms/issues/246); the
  [SolarAssistant Seplos guide](https://solar-assistant.io/help/battery/seplos-bms);
  and an [HA community write-up](https://community.home-assistant.io/t/seplos-bms-v3-mason-10e-all-16-cell-voltages-with-plain-modbus-yaml-no-custom-component-no-esp-no-mqtt/1017847)
  for a single pack.
- **(L)** [ADR-0005](../adr/0005-integration-authority-and-operating-modes.md)
  (one owner per bus), the #321 identity decision
  ([comment](https://github.com/Kylevdm/ha-spark/issues/321#issuecomment-6192455544)),
  [#276](https://github.com/Kylevdm/ha-spark/issues/276), and
  [`solis-modbus-register-map.md`](solis-modbus-register-map.md) for the
  inverter's relayed BMS registers.

## Answer in brief

1. **In a parallel bank, nothing else may poll the pack RS485 bus.** The master
   pack is the Modbus master on the inter-pack RS485 link, and a second master
   collides with it. That is why every v3 tool that works on a parallel bank
   (Seplos3MQTT, seplos_v3_sniffer) only **listens**. Active pollers (ESPHome
   `modbus_controller`, bms_connector, SolarAssistant's active mode) work on a
   single pack or on packs that are not in a master/slave group.
2. **Recommended read path: passive listening on the inter-pack RS485 bus,
   natively in the integration, through a dedicated RS485-to-TCP gateway in
   transparent mode** on a free parallel port of the master or the last pack.
   It gets per-pack PIA (voltage, current, SoC, SOH, capacity, cycles,
   min/max cell), PIB (16 cells, 4 temperatures) and PIC (alarm, FET and
   balancing bits) for every pack, at the master's own polling cadence. It adds
   no traffic and never touches the inverter link. The integration never
   transmits on this bus. The gateway must be configured so it cannot.
3. **Identity is the weak point.** The only serial-like fields are the VIA
   block's **BMS QR code** (0x1715, 30 ASCII bytes) and **Pack QR code**
   (0x1724). Both are marked **R/W** in V0.3. They are integrator-set strings,
   and **the master does not poll VIA**, so a passive listener never sees them.
   Passive frames identify a pack only by its Modbus/DIP address. Under the #321
   rule (unique ids from a hardware serial, never an address), sniffing alone
   **cannot create pack devices**. A serial needs either BLE (the master
   answers VIA reads over BLE) or a one-off active read with the pack out of the
   parallel group. Owner decision needed: see Identity.
4. **BLE is the only documented path that returns both the bank summary (EIA)
   and VIA,** through the master pack's BLE. It is an active request-response,
   but on a vendor-intended side channel (V0.3 lists Bluetooth among the
   devices). It is the best candidate for identity and a good second source.
   It is constrained by BLE range and adapter, one central at a time (the phone
   app is locked out while connected, C), and FW-dependent command support
   (#241).
5. **CAN to the inverter is bank-level only and duplicates what the inverter
   already relays** (Solis 33139 SoC, 33140–33146 BMS V/I/limits). A
   listen-only CAN tap is electrically safe, but it adds little and is the one
   tap that sits on the inverter's BMS link. Not recommended.
6. **Firmware variation is real and documented**: PIA current is 0.01 A on
   ≤200 A boards and 0.1 A on 300/400 A boards (V0.2). EIB offsets differ
   between V0.1 and V0.3. Some FW15 boards reject EIB and SFA reads (#241). A
   standalone pack answers only at address 0x00 (#242). The integration must
   gate on the board rating and treat absent blocks as optional.
7. **v1 split: native.** No existing HA integration covers this bank well.
   bms_connector's v3 path is untested and actively polls with address
   scanning. `bms_ble` returns packs as a list without pack sub-devices or
   serials. The sniffers are ESPHome or MQTT side-cars. A passive RS485 reader
   is a small, deep module that fits ADR-0005's "integration owns device I/O".
   #276's entity-based plan is overtaken for this bank, but a generic
   "BMS entities" source can stay as the fallback for other BMS makes.
8. **JK active balancers**: readable in principle. syssi's `jk_balancer`
   reads a JK RS485 balancer (9600 baud), and `heltec_balancer_ble` reads the
   NEEY/Heltec BLE balancers. They report cell voltages, balancing current and
   status, but **no SoC and no pack current**. They do not talk to the Seplos
   BMS. Their effect on BMS SoC is indirect and inferred, not documented (see
   §7). The exact balancer model must be confirmed before any reader is chosen.

## 1. Ports and topology

Seplos v3 packs have RJ45 sockets for the inter-pack RS485 link ("RS485-1/2",
the parallel ports) and a CAN socket. V0.3 §1 gives the RS485 link as
**19200 8N1**, and the BMS "only responds to requests addressed to its own
address" (V0.3 §1.2). Community pinouts agree on **1/8 = B, 2/7 = A, 3/6 = GND**
on the RS485 sockets
([seplos_v3_sniffer README](https://github.com/DpunktS/seplos_v3_sniffer/blob/d512cb68be2c1f3cc6d9324eb622130b89ce859d/README.md);
HA write-up). The CAN socket carries CAN-H on 4 and CAN-L on 5. On the older 10E
(V16) generation it also carries a separate RS485 pair that reaches only the
master
([privatecoder README](https://github.com/privatecoder/seplos-mqtt-remote-rs485/blob/3a105dfd299c86ec4ff8ddbfec1ec0bf8d91b87e/README.md);
#80, 2023-12-28). The same trick was **not** reproduced on v3 in #80
(2023-12-29: "it doesn't work"), so treat the CAN-socket RS485 on v3 as
unverified.

**Master/slave behaviour (C, consistent across sources).** With more than one
pack, the master pack polls each pack's PIA, PIB and PIC over the inter-pack
RS485 bus. "There can only be one master" (#80, opening post, and ferelarg
2024-01-09). DpunktS reports the packs answer "every 200 ms" (#112,
2024-11-20) and that only the master stays reachable by Bluetooth in a
parallel group (#112, 2024-12-20). SolarAssistant puts it the same way: when
the master is set up for the inverter link, "the master itself [is] not
readable via RS485" and it "occupies the RS485 cables for communication with
the slave batteries". Its advice for that case is **passive reading**
from "an open parallel communication port on the master and on the last
battery".

**Addresses.** V0.3 §2.2 gives BMS ids 0x00–0x7F and serves the EIA/EIB/EIC
summary "by the master pack at ID 0x00" (V0.3, EIA heading). A standalone pack
on current firmware **answers Modbus reads only at 0x00**, whatever its DIP
setting (#242, 2026-08-21). This breaks clients that treat 0 as the Modbus
broadcast address: ESPHome ≥ 2026.8 refuses it, hence the `allow_broadcast_read`
option in
[`esp32-seplos-v3-example.yaml:60`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/esp32-seplos-v3-example.yaml#L60).
In sniffed parallel traffic the packs appear at **0x01, 0x02, …**: the sniffer
accepts 0x01–0x10 and maps 0x01 to "bms0"
([`seplos_parser.cpp:131-132, 168`](https://github.com/DpunktS/seplos_v3_sniffer/blob/d512cb68be2c1f3cc6d9324eb622130b89ce859d/esphome/components/seplos_parser/seplos_parser.cpp#L131-L168)).
The #112 frame dump (2024-10-07) shows the master's request
`02 04 10 00 00 12` and responses from both 0x01 and 0x02. **Which physical pack
answers at 0x01 (the master, or the first slave) is not stated anywhere**, so
it is an open fact.

## 2. Transport options

### 2a. Active Modbus polling of the pack bus: not safe in a parallel bank

- **Port**: a parallel RS485 socket, 19200 8N1.
- **Who does it**: the syssi v3 examples (`modbus_controller`, 10 s
  `update_interval`), the
  [`…-multiple-battery-banks.yaml`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/esp32-seplos-v3-example-multiple-battery-banks.yaml#L66-L78)
  variant (polls 0x00 and 0x01, which is for separate packs, not one parallel
  group), bms_connector v3, and the HA write-up (single pack).
- **Safety**: in a parallel group this makes a second master and collides with
  the pack master. Reported symptoms: "very many CRC errors for the 2nd BMS"
  (#112, opening post), and "the inverter no longer has communication to the
  BMS" with a second adapter (#112, 2024-10-01). bms_connector makes it worse:
  when the configured address is silent it **scans addresses**
  ([`sensors.py:98-127`](https://github.com/flip555/bms_connector/blob/6e6717f349b35587ccc95e105227a86daf04611d/custom_components/bms_connector/bms/seplos/v3/sensors.py#L98-L127)).
  It also imports `telnetlib`
  ([`seplos_v3_local_serial.py:16`](https://github.com/flip555/bms_connector/blob/6e6717f349b35587ccc95e105227a86daf04611d/custom_components/bms_connector/connector/local_serial/seplos_v3_local_serial.py#L16)),
  which Python 3.13 removed.
- **Verdict**: acceptable only for a **single, unparalleled pack**, or for a
  one-off identity read with the bank deliberately broken up (an owner
  procedure, not runtime).

### 2b. Passive listening on the pack bus: recommended

- **Port**: a free parallel RS485 socket on the master or the last pack (B and
  A plus GND). The sniffer README says no 120 Ω terminator was needed.
- **How it works**: decode the master's request/response traffic. The
  responses are fixed-length: PIA `0x24` data bytes (18 registers), PIB `0x34`
  (26 registers), PIC coils `0x12` (18 bytes), each with a Modbus CRC
  ([`seplos_parser.cpp:131-145`](https://github.com/DpunktS/seplos_v3_sniffer/blob/d512cb68be2c1f3cc6d9324eb622130b89ce859d/esphome/components/seplos_parser/seplos_parser.cpp#L131-L145)).
  Seplos3MQTT does the same in Python and decodes request and response pairs
  without ever writing to the port
  ([`seplos3mqtt.py:45, 59, 193-`](https://github.com/ferelarg/Seplos3MQTT/blob/aa14a7054bc69471bd568c40e745f39ebb9be8b1/seplos3mqtt.py#L45);
  the file has no `write` call).
- **What the master asks for**: "the Master asks for everything (except the
  serial number and some other minor things)" (ferelarg, #80, 2024-02-22).
  The VIA block is never on the wire, and neither is EIA (the master *is* the
  EMS).
- **Safety**: the only risk is the listener transmitting. An RS485 transceiver
  that never asserts DE is one receiver load. "Totally passive … does not
  interfere at all in the normal operation" (ferelarg, #80). It works
  alongside the Seplos PC tool and does not touch the inverter link at all,
  whether that link is CAN or RS485.
- **Robustness**: CRC failures happen, and the sniffer simply resyncs.
  Without CRC checking, an earlier version produced wrong values (#112,
  2024-11-20). Buffer sizing matters (2 packs = 266 bytes per cycle, #112).

### 2c. Remote RS485 over TCP (the transport for 2b)

- **Pattern**: an RS485-to-Ethernet gateway in **transparent / raw TCP server**
  mode on the pack bus. The client reads the byte stream and parses it as in
  2b. privatecoder uses a Waveshare 2-CH RS485 to ETH (B) this way with socat
  and a virtual tty, and alienatedsec gives the battery RS485 its own Waveshare
  ("Transfer Protocol: None") apart from the inverter's
  ([README §Waveshare, line 68](https://github.com/alienatedsec/solis-ha-modbus-cloud/blob/744e541257da2fc19f9e9bca462b2024dd115298/README.md?plain=1#L68)).
- **Caveat**: privatecoder's code is **v2/V16 ASCII** (`~…\r` frames, 9600
  baud for multi-pack,
  [`fetch_bms_data.py:443-450, 1025`](https://github.com/privatecoder/seplos-mqtt-remote-rs485/blob/3a105dfd299c86ec4ff8ddbfec1ec0bf8d91b87e/src/fetch_bms_data.py#L443-L450)),
  and v3 firmware ignores it (HA write-up). Only the transport pattern carries
  over.
- **Safety requirements** for a passive listener:
  - The gateway must be a **separate gateway or channel** from the inverter
    bus. It must sit in raw/transparent mode, never Modbus-TCP-to-RTU
    conversion with polling. Gateway caching and any "auto query" feature must
    be off. The integration must never write to the socket.
  - A Modbus-TCP mode would let *any* LAN client inject frames onto the BMS bus
    and into the master's conversation. Raw mode has the same exposure. Treat
    gateway access control as part of the security review.
  - TCP chunking destroys RTU inter-frame timing, so the parser must frame on
    the known lengths plus CRC (as 2b does), not on silence gaps.

### 2d. BLE via the master pack: second source, identity source

- **Port**: the master pack's BLE (service `FFF0`, notify `FFF1`, write `FFF2`;
  [`seplos_bms_v3_ble.cpp:18-20`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/components/seplos_bms_v3_ble/seplos_bms_v3_ble.cpp#L18-L20)).
  The payload is plain Modbus RTU frames.
- **What it gives**: syssi reads EIA, EIB, EIC, **VIA**, PCT, SFA and SPA at
  device 0x00/0xE0. It then reads PIA, PIB and PIC per pack by addressing 0x01,
  0x02, … through the master
  ([`:51-66`, `:756-767`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/components/seplos_bms_v3_ble/seplos_bms_v3_ble.cpp#L51-L66)).
  aiobmsble does the same with EIA/EIB/EIC plus per-pack PIA/PIB. It takes the
  pack count from EIA
  ([`seplos_bms.py:43-56, 206-250`](https://github.com/patman15/aiobmsble/blob/2a78c0e0ad9ce018236bd39622bf77c21b20d26f/aiobmsble/bms/seplos_bms.py#L43-L56)),
  and its frame builder allows devices 0x00–0x10, 0xC0 and 0xE0 (`:195`).
- **Safety**: no shared wire. These reads are the vendor app's own path; V0.3
  §2.2 lists Bluetooth ids 0xE0, 0x00–0x10 and 0xC0. Whether the master answers
  per-pack reads from its cache or polls the slave on demand is not
  documented. Either way the master arbitrates, so nothing collides. One
  central at a time: an HA BLE connection excludes the phone app (C). The
  syssi component reads SPA and SFA settings blocks. ha-spark must not, both
  to avoid an over-broad surface and because reading those blocks has no use
  here. It also never sends FC 0x10/0x0F (SCA, V0.3 §SCA: "A monitoring tool
  should never write them").
- **Weakness**: range and adapter quality (BMS_BLE-HA warns about the
  Raspberry Pi internal adapter), and firmware gaps (#241: FW15 rejects EIB
  0x2100 and SFA 0x1400 with exception 0x02).

### 2e. CAN to the inverter: not recommended

- **Port**: the master's CAN socket (4/5), already in use for the inverter
  link if that link is CAN. Whether the Solis inverter in the household takes
  CAN or RS485 from the master is **not confirmed**.
- **What it gives**: inverter-protocol frames (Pylon/Growatt style),
  bank-level only. Seplos notes that in Growatt CAN mode a parallel bank stops
  sending per-cell frames 0x315–0x318, and 0x319 carries min/max across packs
  ("Notes from Seplos", V0.3 md lines 26–31). No per-pack data and no serial.
- **Safety**: a CAN controller in listen-only mode sends nothing, not even
  ACKs, and does not add bus load. In normal mode it ACKs and can send error
  frames, which is unacceptable on the inverter's BMS link.
- **Value**: low. The inverter already relays BMS SoC, voltage, current and
  limits on registers the integration polls anyway (33139–33146,
  `solis-modbus-register-map.md` §1a). A CAN tap gives the same
  numbers one hop earlier, at the cost of the riskiest wiring.

### 2f. Sniffing the inverter's RS485 BMS link

If the inverter link is RS485, the same passive technique applies. But it only
carries the master's inverter-protocol summary, so it has 2e's low value with
2b's wiring. Not recommended.

## 3. Identity

| Candidate | Where | Stable? | Seen passively? |
|---|---|---|---|
| Modbus/DIP address | frame byte 0 | configuration, not hardware | yes |
| Manufacturer name | VIA 0x1700, ASCII 20, R | vendor string | no |
| Device name | VIA 0x170A, ASCII 20, **R/W** | user-settable | no |
| Firmware version | VIA 0x1714, ASCII 2, R | changes on update | no |
| **BMS QR code** | VIA 0x1715, ASCII 30, **R/W** | factory board label; writable | no |
| **Pack QR code** | VIA 0x1724, ASCII 30, **R/W** | integrator-set | no |

Sources: V0.3 §VIA (md lines 440–449). The V0.1 PDF has no VIA block. syssi
publishes 0x1715 as "BMS SN" and 0x1724 as "Pack SN"
([`seplos_bms_v3_ble.cpp:48, 515-525`](https://github.com/syssi/esphome-seplos-bms/blob/7595c6050ac443397a673a9cbcb107623469e79f/components/seplos_bms_v3_ble/seplos_bms_v3_ble.cpp#L515-L525)).
One reported value, `ESSPro16kWh-260413-XXXX` (#241), is plainly an
integrator's pack label, not a board serial. Neither aiobmsble nor bms_connector
reads VIA. bms_connector keys entities on the config-entry id
([`sensors.py:376-377`](https://github.com/flip555/bms_connector/blob/6e6717f349b35587ccc95e105227a86daf04611d/custom_components/bms_connector/bms/seplos/v3/sensors.py#L376-L377)).

Consequences for #321:

- The **BMS QR code (0x1715)** is the best hardware id available. It is the
  board's factory QR. It is writable, but only through the vendor tool, and
  ha-spark never writes it. Apply #321's rule as for inverters: blank, garbled
  or **duplicate-within-bank** values mean the pack is **not adopted** as a
  device. Whether 0x1715 is populated and unique on these two boards is an
  open hardware fact.
- **Passive sniffing alone cannot supply it.** Options, for the owner to
  choose:
  - **(i) BLE identity, passive data.** Read VIA over BLE (0x00 for the
    master; whether a VIA read addressed to 0x02 through the master returns the
    slave's VIA is untested), bind serial ↔ bus address, then take data from
    the sniffer. A changed serial at an address raises a repair, as with
    inverter replacement.
  - **(ii) BLE only**, for identity and data. One transport, but every reading
    depends on BLE.
  - **(iii) No pack devices.** Pack readings live on the bank device as
    `<bank id>_pack<n>_<key>` entities keyed by bus position. This is honest
    about what a sniffer knows, but it does not detect a pack swap.
- **Master vs slave** is not in any frame. It is implied by EIA (served by the
  master) and by which pack answers BLE. Record it as a pack attribute, never as
  part of the id: the role can change if DIP settings change.

## 4. Fields and freshness

Per pack, from the passive frames (V0.3 PIA/PIB/PIC; units as V0.3):

| Need | Register | Unit / note |
|---|---|---|
| Pack voltage | PIA 0x1000 | 0.01 V |
| Pack current | PIA 0x1001 | INT16, **0.01 A on ≤200 A boards, 0.1 A on 300/400 A** (V0.2 note; #246) |
| Remaining / full capacity | 0x1002 / 0x1003 | 0.01 Ah (a 280 Ah pack fits in UINT16 at this unit) |
| SoC / SOH | 0x1005 / 0x1006 | 0.1 % |
| Cycles | 0x1007 | count |
| Min/max/avg cell V, temps | 0x1008–0x100D | mV; 0.1 K |
| Charge/discharge current limits | 0x100F / 0x1010 | A |
| 16 cell voltages, 4 cell temps, ambient, MOSFET | PIB 0x1100–0x1119 | mV; 0.1 K |
| Alarms, FET state, balancing bits, system state | PIC coils 0x1200–0x1288 | bitfields (tables 4.1–4.10) |

The bank summary (EIA: bank voltage, INT32 current at 0.1 A, SoC, online-pack
flags 0x200C, pack count 0x2016) is available **only over BLE**. A passive
listener derives bank totals itself.

**Freshness**: no BMS field carries a sample timestamp. SCA 0x1500 is the RTC,
it is not polled by the master, and HIA is history. Freshness is therefore the
**arrival time of the last CRC-valid frame per address and block**. The
master's cadence (about 200 ms per DpunktS) gives a tight staleness bound.
Silence from one address while others continue means that pack is
offline or out of the group. Silence from all means the master or listener
is down. These are distinct health states. Over BLE, freshness is the
request/response round-trip, and EIA 0x200C reports which packs the master
considers online.

**Cross-checks available for BMS measurement integrity**: per-pack SoC against
per-pack remaining/full capacity; Σ pack current against the inverter's
33134/33135 and BMS 33142; pack voltage against 33133; per-pack SoC spread.
Divergence between two nominally identical packs is exactly the "configured
differently" case in CONTEXT.md, and the per-pack view makes it visible.

## 5. Protocol and firmware variation

| Variation | Evidence | Integration response |
|---|---|---|
| v2/V16 (10C/10E) use the ASCII `~` protocol; v3 uses Modbus RTU | privatecoder code; HA write-up ("3.0 firmware ignores the older ASCII protocol") | v3 only. Detect by frame shape. |
| PIA current unit by board rating | V0.2 revision note; #246 (open, unconfirmed on hardware) | Require the board rating (or infer it from the model in VIA 0x170A) before trusting current. Cross-check with Σ current against the inverter. |
| EIB offsets differ V0.1 vs V0.3 (e.g. max pack SoC 0x2112 vs 0x210D) | V0.1 PDF p.9 vs V0.3 §EIB | Avoid EIB, or gate it on the document version. Derive from per-pack data. |
| FW15 boards reject EIB and SFA | #241 (BMS16S200A-ST001, FW 15) | Treat exception 0x02 as "block absent", not as an error. |
| Standalone pack answers only at 0x00 | #242 | Relevant only to the one-off active identity read. |
| EMS device id 0x00 (Chinese) vs 0xB0–0xBF / ECU 0xC0 (English) | V0.3 md "official English edition" | BLE path: use 0x00 as syssi and aiobmsble do. |
| Verified hardware | syssi README: B-48200-C (BMS16S200A-SP05B, FW 1.3); sniffer: 2–4 packs (#112) | Record the models and firmware we verify. Claim nothing broader. |

## 6. Fit with the v1 split

| | Native in the integration | Consume another integration's entities (#276) |
|---|---|---|
| Candidates | passive RS485 listener over a TCP gateway (2b/2c); optional BLE via HA's bluetooth stack (2d) | `bms_ble` (BLE), bms_connector (active RS485), ESPHome sniffer or MQTT side-cars |
| Safety on a parallel bank | listener never transmits, which we can prove in code and tests | bms_connector is unsafe (second master, address scan); `bms_ble` is fine; side-cars are fine |
| Pack devices and identity | can follow #321 (serial-gated) | `bms_ble` = one device per BLE MAC, packs not sub-devices (its README lists "sub-devices … multiple packs" as future work); side-cars have no serials |
| Freshness | per-frame arrival, exact | entity `last_updated`, which only moves on change unless forced |
| Install burden | one gateway (the same make as the inverter's) plus config | extra integration or ESP plus YAML; the owner maintains it |
| Code size | small: fixed-length frame parser + CRC + 3 decoders | none, but a preset per source |

Recommendation: **native passive listener**, with BLE as an optional identity
and second source. It sits beside the Solis bus in the same integration, so
pack data and inverter data share one clock. ADR-0005's "sole client on each
bus" needs a one-line amendment for this case: on a BMS bus the master pack
owns the bus, and the integration is a **non-transmitting listener**, not a
client. #276's entity-based path can stay as a generic fallback for other BMS
makes. Flagged, not changed here.

Tradeoffs:

- Passive gives no serials, so pack devices need BLE or the owner's choice of
  (iii).
- Needs a second RS485 gateway (or channel) and a cable to a free parallel
  port.
- Raw-TCP parsing must handle chunking and resync, which the reference
  implementations show is manageable.
- The data cadence is the master's, not ours. That is fine for verification
  and is the reason it is safe.

## 7. JK active balancers

- **Readable?** It depends on the exact model, which is unconfirmed.
  - syssi's [`jk_balancer`](https://github.com/syssi/esphome-jk-bms/blob/25822a8f9ab784fa89ccdd1f3002f8e4a2e06916/components/jk_balancer/jk_balancer.cpp#L26-L120)
    reads a JK RS485 balancer at 9600 baud
    ([example](https://github.com/syssi/esphome-jk-bms/blob/25822a8f9ab784fa89ccdd1f3002f8e4a2e06916/esp32-active-balancer-example.yaml)).
    Its status frame (function 0xFF) has total voltage, average/min/max cell,
    per-cell voltages, max delta, **balance current**, trigger voltage,
    balance state (idle/charging/discharging) and alarms. The same component
    also defines write functions (0xF0, 0xF2, 0xF4: cell count, trigger,
    current), which ha-spark must not use.
  - NEEY/Heltec "smart active balancers" (often sold under other labels) are
    read over BLE by `heltec_balancer_ble` (README supported list).
  - Newer JK BMS boards with built-in balancing use `jk_bms_ble`. That
    applies only if the "balancer" is actually a JK BMS.
- **SoC fields?** None. A balancer has no shunt and does not compute SoC.
- **Does it affect the BMS's SoC?** No direct path: it does not talk to the
  Seplos BMS, and the BMS shows no external-balancer input (PIC balancing bits
  are its own passive balancer). Indirect effects are **inferred, not sourced**:
  - Its quiescent and conversion losses are drawn from the cells **outside the
    BMS shunt**, so coulomb counting does not see them. That causes slow SoC
    drift until the next full-charge recalibration.
  - By flattening cells it changes when the highest cell reaches the
    full/over-voltage thresholds (SPA 0x130A–0x130D), which can move the
    top-of-charge recalibration point.
  - Both are BMS measurement integrity evidence, not capacity change, and both
    belong to the calibration trust gate.
- **Fit**: a balancer reader is optional diagnostics (cell spread, balancing
  activity). It is not needed for SoC verification. Keep it out of v1 unless
  the owner wants it.

## Open questions (need a hardware test)

1. **Inverter link type**: does the Solis take CAN or RS485 from the master?
   Read-only check of the master's PCT 0x1800–0x1822 over BLE, or the cabling.
2. **Which pack answers at 0x01** in sniffed traffic, master or slave?
   Compare sniffed pack voltage and current with the BLE per-pack read or the
   pack display.
3. **0x1715 BMS QR code** on both boards: populated, printable, distinct?
   Read VIA over BLE at 0x00. Then test whether a VIA read addressed to 0x02
   through the master BLE returns the slave's.
4. **Board current rating** (≤200 A versus 300 A) for the PIA current unit.
   Check against the label or the VIA device name, and sanity-check
   Σ current against inverter 33134.
5. **Firmware version** (VIA 0x1714) on both packs, and whether EIB/SFA
   answer (#241).
6. **Free parallel port** on the master or last pack, and its pinout (1/8 B,
   2/7 A, 3/6 GND). Confirm a raw-TCP gateway sees CRC-valid PIA/PIB/PIC frames
   from both addresses and the observed cadence.
7. **BLE reachability** from the HA host or a proxy, and the effect on the phone
   app while connected.
8. **JK balancer exact model**, and whether it has RS485 or BLE.

## Out of scope

Any BMS write (SPA, SFA, SCA, PCT 0x1800/0x1823, VIA R/W fields, CAN/485
protocol switch commands) is out of scope, as are cell calibration and
history-record reads (HIA uses non-standard register counts 0x55AA/0xAA55).
ha-spark never writes BMS settings.
