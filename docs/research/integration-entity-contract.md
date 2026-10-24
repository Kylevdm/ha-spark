# Research: the integration's entity contract — what the settled decisions already require

Date: 2026-10-14
Ticket: [#308](https://github.com/Kylevdm/ha-spark/issues/308), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).

This note decides nothing. It collects what the closed map tickets already
require of the companion integration's entities, services and commands, puts it
into one draft table, and lists what is still open. Every claim cites a
decision comment, a repo `file:line` (at `origin/master` `96bbc22`), or an
official Home Assistant source. HA Core source is cited at commit
`1e76a638e0a73e689116e20452d803f74c9bf9c9` (`dev`, 2026-10-14).

## TL;DR

- **No actuation service exists in the settled contract.** The add-on never
  actuates through HA services. The integration fetches add-on commands over
  the bearer-authenticated discovery channel, and the only add-on-facing
  service is a data-free `commands pending` nudge
  ([#291](https://github.com/Kylevdm/ha-spark/issues/291#issuecomment-6142935741);
  `docs/adr/0008-operating-mode-and-authority-lifecycle.md:42-47`). The
  integration's other services are owner services (→ Observe, manual handover
  or resume) for non-system HA admins (`0008:59`). There is no override service
  (`docs/adr/0009-planner-mediated-overrides.md:42`) and no heartbeat service
  (`0008:38`).
- **State and authority are read-only sensors.** Mode, the manual session and
  the override are sensors, "never `select` or `switch` entities" (`0008:59`;
  `0009:42`).
- **Device entities are the only HA write surface.** Each write is gated per
  mode by who owns the control, then validated against limits, read back and
  logged (`0008:61`). The integration owns read-back
  ([#249](https://github.com/Kylevdm/ha-spark/issues/249#issuecomment-6059216761);
  `docs/adr/0005-integration-authority-and-operating-modes.md:10-11`).
- **Today the add-on pushes 14 `sensor.ha_spark_*` states over REST
  `POST /api/states`**, and HA forgets them on restart (`ha_spark/energy/publish.py:5-8`,
  `ha_spark/energy/v2l.py:225-250`). It also writes the device through four HA
  services (`modbus.write_register`, `select.select_option`,
  `homeassistant.update_entity`, `alphaess.setbatterycharge`). The sole-bus-owner
  rule removes all four (`0005:86-90`). No decision yet says what feeds the plan
  sensors.
- **Two facts the contract has to respect.**
  - Removing the add-on deletes a `hassio`-source config entry whose
    `unique_id` equals the discovery uuid (Core `hassio/discovery.py:157-160`).
  - HA's Assist exposes every `switch` entity by default, unless the entity has
    an `entity_category` or is hidden (`homeassistant/exposed_entities.py:32-44, 304-316`).
- **The section below lists 18 open decisions.** Each one is sharp enough to
  become a grilling ticket.

## 1. What the add-on writes to HA today

### 1a. REST `set_state` (plan state)

The `set_state` helper is `POST /states/{entity_id}` (`ha_spark/ha/rest.py:106-112`).
HA's docs say this endpoint "sets the representation of a device within Home
Assistant and will not communicate with the actual device". It also overwrites
an existing state, returning 200 instead of 201
([REST API](https://developers.home-assistant.io/docs/api/rest/)).

| Entity today | Source | Notes |
|---|---|---|
| `sensor.ha_spark_charge_needed_kwh` | `publish.py:71-79` | SoC-derived, set to `unavailable` when SoC is untrusted (`publish.py:29-46, 163`) |
| `sensor.ha_spark_target_soc` | `publish.py:80-88` | SoC-derived |
| `sensor.ha_spark_soc_now` | `publish.py:89-97` | SoC-derived |
| `sensor.ha_spark_forecast_load_kwh` | `publish.py:98-106` | |
| `sensor.ha_spark_solar_forecast_kwh` | `publish.py:107-115` | |
| `sensor.ha_spark_deficit_kwh` | `publish.py:116-124` | SoC-derived |
| `sensor.ha_spark_plan_status` | `publish.py:59-68, 125-129` | attributes carry `proactive_mode`, `soc_status`, export SoC need |
| `sensor.ha_spark_last_run` | `publish.py:130-137` | timestamp |
| `sensor.ha_spark_planned_cost` | `publish.py:139-150` | conditional; GBP |
| `sensor.ha_spark_baseline_cost` | `publish.py:151-162` | conditional; GBP |
| `sensor.ha_spark_soc_integrity` | `publish.py:186-241` | per minute; attributes include `proactive_mode` and every `fallback_*` field |
| `sensor.ha_spark_predictions` | `publish.py:244-261` | a list attribute of decisions |
| `sensor.ha_spark_v2l_power_w` / `_energy_kwh` / `_net_saving_gbp` | `v2l.py:225-250`, pushed at `v2l.py:524-528` | |

The daemon calls these at `ha_spark/energy/scheduler.py:304, 327, 793, 839, 920`.
The add-on API already serves the plan list "so the companion integration
mirrors them" (`publish.py:53-57`; `GET /api/plan` at `ha_spark/api/server.py:363-373`).

### 1b. Service calls

| Call | Where | Purpose | v1 fate |
|---|---|---|---|
| `modbus.write_register` | `ha_spark/devices/inverters/solis.py:1241-1246` | 43141/43142 currents, 43143+ slot blocks, 43000 clock sync (`solis.py:80-84, 1043`) | Removed: the integration is the sole Modbus client (`0005:86-90`) |
| `select.select_option` | `solis.py:892-897` | 43007 power switch via overlay select (hold reconcile, safe state) | Removed, as above |
| `homeassistant.update_entity` | `solis.py:1384-1387` | Forces an overlay refresh before read-back | Removed: read-back moves into the integration ([#249](https://github.com/Kylevdm/ha-spark/issues/249#issuecomment-6059216761)) |
| `alphaess.setbatterycharge` | `ha_spark/devices/inverters/alphaess.py:72-82` | Cloud charge window and stop SoC, with no read-back | Replaced by local Modbus (map #247 "Legacy v1 carry-over"; [#250](https://github.com/Kylevdm/ha-spark/issues/250)) |
| `notify.<service>` | `ha_spark/ha/rest.py:16-18`; `v2l.py:532` | Phone notifications | Not actuation; who sends what is open (D16) |

Today's read-back takes up to 16 reads 1 s apart, and needs a second matching
read 5 reads later to confirm a zero (`solis.py:100-111`). Each action reports
`[APPLIED]`, `[WARNING] … but <mismatch>`, `[FAILED]`, `[SKIP]` or `[BLOCKED]`
(`solis.py:885-890, 195, 258, 313`).

## 2. Constraints from the settled decisions

**Split and transport**
- C1. The integration owns device I/O, entities, command validation, limits,
  write authority and read-back. Without the add-on, it still exposes entities
  and accepts authorised manual changes, with a warning (`0005:10-15`; map #247 Notes).
- C2. The integration is the only Modbus client on each bus. The add-on has no
  direct Modbus connection (`0005:86-90`). The bus needs about 45 registers in
  5–6 frames, and gateway caching must be off ([#249](https://github.com/Kylevdm/ha-spark/issues/249#issuecomment-6059216761)).
- C3. Add-on commands are never HA services. The integration fetches them from
  the add-on, posts each result back, and the data-free `commands pending`
  service only prompts a fetch (`0008:42-44`).
- C4. Each command carries the session id, the epoch and a sequence number. The
  integration rejects *stale session*, *stale authority* and *replayed*
  commands, and results name the sequence (`0008:47, 55`).
- C5. The bearer token is per add-on boot and kept in memory only in the
  add-on. The integration stores it in the config entry, compares it in
  constant time and redacts it from logs and diagnostics (`0008:46`).
- C6. Discovery is accepted only from ha-spark's slug. Standalone installs take
  host, port and token from the config flow (`0008:45`). An `api_version` skew
  raises a repair, not a block ([#252](https://github.com/Kylevdm/ha-spark/issues/252#issuecomment-6059187751);
  [#257](https://github.com/Kylevdm/ha-spark/issues/257#issuecomment-6132772796) step 2).

**Mode, authority and manual state**
- C7. One site-wide mode. The integration's durable store is the only source
  of truth for the mode, the manual session, fallback causes, any transition
  in progress, and the epoch. The add-on keeps no copy (`0008:7-9`).
- C8. Mode and manual session are read-only sensors, "never `select` or
  `switch` entities, so dashboards and Assist cannot change them" (`0008:59`).
- C9. Owner actions on integration services need a non-system HA admin. No-user
  and non-admin calls are refused for owner actions. The integration's own HA
  UI can always reach → Observe and manual handover; entering Optimise needs
  the add-on (`0008:59`; [#291](https://github.com/Kylevdm/ha-spark/issues/291#issuecomment-6142935741) "Other callers").
- C10. Device-entity writes are gated by mode:

  | Mode | Who may write device entities |
  |---|---|
  | Observe | Any HA caller |
  | Protect | Any HA caller, except protection-owned controls |
  | Optimise or fallback | ha-spark-owned controls refused |
  | Manual session | Open, except kept-protection controls |

  A refusal is a service error naming the owner (`0008:61`).
- C11. *Authority unknown* (missing or corrupt store) means no writes of any
  kind, plus a repair (`0008:75`).
- C12. The override lives in the add-on's store and doesn't bump the epoch. The
  integration exposes a read-only override sensor and **no service** (`0009:38, 42`).

**Config**
- C13. Connection data lives in config-entry data, and limits plus the fallback
  schedule in config-entry options. Manual state is durable runtime state,
  edited only through authority commands (`0006:11-13`). Ingress edits
  integration fields through one validated integration command with the
  expected revision (`0006:26`). Integration secrets stay in its config entry
  (`0006:20`).
- C14. Agent-editable integration fields commit only on an owner's ingress
  approval of an immutable proposal. That commit goes through the same
  validated, revision-checked integration command (`0006:36`;
  [#268](https://github.com/Kylevdm/ha-spark/issues/268#issuecomment-6111196558);
  [#278](https://github.com/Kylevdm/ha-spark/issues/278#issuecomment-6133071314)).
- C15. Major-load demand is owned by the integration, and the add-on reads it.
  Every Settings section saves to exactly one store, and a Re-probe gap raises
  a repair ([#306](https://github.com/Kylevdm/ha-spark/issues/306#issuecomment-6177623171)).

**Safeguards the integration runs**
- C16. The supply guard runs in the integration on its own loop. One live clamp
  caps every charge write at min(requested, live allowance). It is
  event-driven: a 2-reading cut, a 5-minute restore, and a daily budget of 48
  restores. In Observe it only warns. A reading counts as missing after 60 s
  ([#165](https://github.com/Kylevdm/ha-spark/issues/165#issuecomment-6118621456) d1–d6).
- C17. The device export bound is read, never written. Scheduled export needs
  the bound on and ≤ the site export limit, with the sum rule across inverters.
  A 2-reading tripwire stops export and marks the bound unvalidated. The bound
  is re-verified before each window
  ([#283](https://github.com/Kylevdm/ha-spark/issues/283#issuecomment-6133231536) d2, d3, d6, d8).
  On the reference Solis, 43073 needs a read-modify-write
  ([#297](https://github.com/Kylevdm/ha-spark/issues/297#issuecomment-6145615838)).
  AlphaESS fails the gate on present evidence
  ([#296](https://github.com/Kylevdm/ha-spark/issues/296#issuecomment-6142221487)).
- C18. The integration runs protection holds, including during add-on loss.
  Holds outrank fallback. Unknown evidence starts no hold, and a confirmed EV
  hold needs five fresh clear reports or a handover
  (`docs/adr/0007-protection-evidence-and-outage-boundary.md:5-9`).
  Freshness needs successful-read evidence, not HA timestamps
  ([#285](https://github.com/Kylevdm/ha-spark/issues/285#issuecomment-6125943045)).
- C19. Fallback has two causes, applies only in Optimise, and is validated at
  save. It is "in force only once read-back matches" (`0008:65-71`; `CONTEXT.md:274-284`).
- C20. The Solcast client counts its own quota and persists the last forecast.
  It supplies tomorrow's 48 undampened half-hour slots at P10/P50/P90, summed
  across sites, plus freshness and quota metadata
  ([#251](https://github.com/Kylevdm/ha-spark/issues/251#issuecomment-6059172965);
  `docs/research/solcast-api.md:175-213`).

## 3. Home Assistant conventions that apply

| Convention | Source | Consequence |
|---|---|---|
| `has_entity_name = True` is mandatory for new integrations. `name` excludes the device name. The main feature has `name = None`, and the friendly name becomes `"{device.name} {entity.name}"` | [Entity](https://developers.home-assistant.io/docs/core/entity/); Bronze `has-entity-name` | Entity ids derive from device plus entity name, not today's `sensor.ha_spark_*` (D5) |
| `unique_id` must be unique within a platform and not changeable by the user. Acceptable sources are a serial number, a MAC address or a printed or EEPROM id. Unacceptable sources are an IP address, a device name, a hostname or a URL. The config entry id is a last resort | [Entity registry](https://developers.home-assistant.io/docs/entity_registry_index); Bronze `entity-unique-id` | Solis serial at 33004–33019 (`docs/research/solis-modbus-register-map.md:132, 395`). No AlphaESS serial register is identified in `docs/research/alphaess-local-modbus.md`. The gateway has no Modbus identity (D2) |
| A device "represents either a physical device that has its own control unit, or a service". `via_device` is for hubs, and `entry_type` may be `service` | [Device registry](https://developers.home-assistant.io/docs/device_registry_index); Gold `devices` | A battery bank is neither, and the site is plausibly a service device (D1) |
| `EntityCategory.CONFIG` for settings and `DIAGNOSTIC` for diagnostics. Noisy entities are disabled by default | [Entity](https://developers.home-assistant.io/docs/core/entity/); Gold `entity-category`, `entity-disabled-by-default` | Also controls Assist default exposure (row below) |
| Default Assist exposure: domains `climate, cover, fan, humidifier, light, media_player, scene, switch, todo, vacuum, water_heater`, plus some binary_sensor and sensor device classes. Never exposed by default: anything with an `entity_category` or `hidden_by`. The first evaluation is stored | Core `homeassistant/components/homeassistant/exposed_entities.py:32-65, 247-271, 304-334` | A `switch` device control is exposed by default; `number`, `select` and `time` are not (D7) |
| `extra_state_attributes` should explain the state. Use fewer attributes or more sensors on chatty entities, and mark the rest `_unrecorded_attributes` | [Entity](https://developers.home-assistant.io/docs/core/entity/) | Applies to the `predictions` list and the Solcast slots |
| The recorder drops state attributes over 16,384 bytes | Core `recorder/db_schema.py:90, 590-596` | Bounds any attribute-borne forecast or timeline (D14) |
| Service actions are registered in `async_setup`, and raise `ServiceValidationError` when the entry isn't loaded | Bronze [`action-setup`](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup); Silver `action-exceptions` | Applies to `commands pending` and the owner services |
| Diagnostics must redact passwords, API keys, tokens, location and personal data with `async_redact_data` | [Diagnostics](https://developers.home-assistant.io/docs/core/integration_diagnostics); Gold `diagnostics` | Discovery token (`0008:46`), Solcast key, gateway host and serials (D17) |
| Other rules: `entity-unavailable` and `log-when-unavailable` (Silver); `repair-issues`, `reconfiguration-flow`, `entity-translations`, `stale-devices`, `dynamic-devices` (Gold); `strict-typing` (Platinum) | [Rules list](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/) | Target tier is open (D17) |
| A `hassio` discovery removal deletes entries with `source == hassio` **and** `unique_id == discovery uuid` | Core `homeassistant/components/hassio/discovery.py:142-160` | Removing the add-on would delete the integration's entry under that `unique_id` choice, which breaks C1 and the removal handover at `0008:77` (D3) |

## 4. Draft entity contract

*Owner* is who holds the value. *R/W* is the HA-facing write surface. Platforms
and names are drafts. Where no decision fixes a choice, the row says so.

### Site device

| Entity (draft) | Platform | R/W | Value owner | Required by | Replaces today |
|---|---|---|---|---|---|
| Operating mode (`observe`/`protect`/`optimise`, plus *activating*, *switching to Observe*, *authority unknown*) | `sensor` (enum) | RO | Integration store | C7, C8, C11; `0008:19-26`; `0005:77-82` | `proactive_mode` attributes (`publish.py:62, 219, 250`) |
| Manual session (deadline, return-to mode, protection kept) | `sensor` (timestamp + attributes) | RO | Integration store | C7, C8; `0008:28, 34` | — |
| Fallback active and its causes | `binary_sensor` + attribute, or enum `sensor` | RO | Integration store | C19; `0005:52` "show fallback active" | `fallback_*` attributes of `soc_integrity` (`publish.py:220-237`) |
| Add-on connection (session live, last heartbeat) | `binary_sensor` (connectivity) | RO | Integration | C1 "warns"; `0008:38` | — |
| Authority epoch | diagnostic `sensor` (or diagnostics only) | RO | Integration store | `0008:55` | — |
| Override (kind, target, end, suspended reason) | `sensor` | RO | **Add-on store**, mirrored | C12 | — |
| SoC integrity | `sensor` | RO | Either side detects (`0008:65`) | `publish.py:186-241` | `sensor.ha_spark_soc_integrity` |
| Supply guard (live allowance, cut active, restores left) | `sensor` / `binary_sensor` | RO | Integration | C16 | — (not published today) |
| Hold (active, cause, trusted end) | `binary_sensor` + attributes | RO | Integration | C18; `CONTEXT.md:212-221` | — (implied by the power switch today) |
| Protection evidence freshness per source | diagnostic `sensor`s | RO | Integration | C18 | — |
| Solar forecast (tomorrow P10/P50/P90 kWh, 48 slots, fetched-at, quota) | `sensor`(s) | RO | Integration (Solcast I/O) | C20 | replaces the consumed Solcast entity read via `solar_tomorrow_entity` (`solcast-api.md:149-173`) |
| Plan sensors (charge needed, target SoC, SoC now, forecast load, solar used, deficit, status, last run, planned/baseline cost, predictions) | `sensor` | RO | **Add-on**, mirrored | map #247 (integration owns ha-spark's entities); `server.py:363-373` | the 12 `publish.py` sensors |
| V2L power / energy / net saving | `sensor` | RO | Add-on | `v2l.py:225-250` | the 3 `v2l.py` sensors |
| Major-load demand, site limits, fallback schedule | config-entry options, not entities | via options flow or integration command | Integration | C13, C15; `0008:69` | add-on options today |

### Inverter device (one per inverter)

| Entity (draft) | Platform | R/W | Required by | Register / today |
|---|---|---|---|---|
| Battery SoC | `sensor` (battery) | RO | planner input; C11 trusted SoC | Solis 33139; AlphaESS `0x0102` (`alphaess-local-modbus.md:103`) |
| Battery voltage, battery power and direction | `sensor` | RO | BMS integrity; C18 direction evidence | Solis 33133, 33135, 33149–33150; AlphaESS `0x0100`, `0x0126` |
| Grid-port power | `sensor` (power) | RO | C16 guard measurement | Solis 33151–33152 (`solis-modbus-register-map.md:149`) |
| Grid charge permitted (work-mode bit 5) | `binary_sensor` | RO, never written | `solis.py:112-114` | Solis 33132 |
| Inverter clock and clock-agreement repair | diagnostic `sensor` + repair | RO; the sync is an action (D8) | `0008:69`; `CONTEXT.md:193-199` | Solis 33022–33027; sync 43000–43005 |
| Identity and firmware gates (model, TOU version, Remote Dispatch capability) | device info + diagnostic | RO | [#249](https://github.com/Kylevdm/ha-spark/issues/249#issuecomment-6059216761); [#259](https://github.com/Kylevdm/ha-spark/issues/259#issuecomment-6114016256) | 33000, 35000, 33289, 34502 |
| Timed charge current, timed discharge current | `number` | **RW**, mode-gated (C10), clamped (C16) | `0008:61`; C16 | 43141 / 43142 |
| Charge and discharge windows (slots 1–3) | `time` entities or one compound control (D6) | **RW**, mode-gated | `0008:61` | 43143–43170. The 8-register block write *is* the commit (`solis.py:15-18`) |
| Power switch (whole-inverter enable; hold surface) | `select` (today's overlay) or `switch` (D7) | **RW**, protection-owned in Protect | C10, C18; `solis.py:766-797` | 43007 (190 on / 222 off) |
| Device export bound (switch, setpoint, read-back) | `binary_sensor` + `sensor` | **RO by decision** | C17 | 43073 bit 4, 43074, 33247 |
| Export bound validated | `binary_sensor` | RO | C17 d6, d8 | — |
| AlphaESS time-period control and dispatch | `number` / `time` / `select` | **RW** (dispatch use is gated by hardware tests) | [#250](https://github.com/Kylevdm/ha-spark/issues/250); `0007:17` | `0x084F–0x0861`, `0x0880–0x088A` |
| AlphaESS feed-in percent | `sensor` | RO | [#296](https://github.com/Kylevdm/ha-spark/issues/296#issuecomment-6142221487) | `0x0800` |

### Battery bank and gateway

| Device | Entities (draft) | Required by |
|---|---|---|
| Battery bank | Bank SoC (shared); bank limits as options; charging allocation (RO) | `CONTEXT.md:132-138, 157-159`; [#241](https://github.com/Kylevdm/ha-spark/issues/241) |
| RS485 gateway | Connectivity; caching-mode probe result (diagnostic), raising a repair when caching is on | [#259](https://github.com/Kylevdm/ha-spark/issues/259#issuecomment-6114016256); #282; C15 |

### Services and commands

| Surface | Caller | Data | Required by |
|---|---|---|---|
| `commands pending` service | Anyone | **None** | C3 |
| Owner services: → Observe, start or resume manual control (other transitions open, D8) | Non-system HA admin only | Deadline choice (1 h, until tomorrow at the household-clock time, custom) | C9; `0005:34-36`; `0008:34` |
| Device-entity writes (`number.set_value`, `time.set_value`, `select.select_option`, …) | Any HA caller, gated by C10 | Validated, clamped, read back, logged with caller context | `0008:61` |
| **Add-on command** (over the channel, not a service): plan apply actions | The add-on | session, epoch, sequence + action payload (D10) | C3, C4 |
| Add-on command: authority transition for an ingress or CLI owner | The add-on, naming the `X-Remote-User-Id` user | expected epoch | `0008:55, 59` |
| Add-on command: integration config update (Settings, Setup, approved agent proposal) | The add-on | expected revision; whole-document validation | C13, C14 |
| Add-on command: Re-probe | The add-on, admin-only | — | C15 |
| Not offered | — | Override, heartbeat, plan-apply or config services | C12; `0008:38, 42` |

**Read-back semantics settled so far.** The integration verifies every write by
reading it back (C1, `0008:61`). A fallback or plan counts as verified only
when every action reads back (C19; `0008:71`). Entering Optimise stays
*activating* until the first fresh plan is fully applied and verified
(`0008:20`). Read-back through a caching gateway reads the gateway's store, not
the inverter, so caching off is a precondition
([#259](https://github.com/Kylevdm/ha-spark/issues/259#issuecomment-6114016256)).
The numeric definition of "verified" (attempts, timing, zero confirmation) is
not settled for the integration (D9).

## 5. Open decisions

Decisions 4, 11, 12 and 15 are resolved in [Integration plan sensors, safeguard entities, and how state crosses the boundary](https://github.com/Kylevdm/ha-spark/issues/322) (ADR-0008 amendment).

1. **HA device model.** Which HA devices exist (site, inverter, battery bank,
   gateway), and the `via_device` chain (inverter via gateway?). Is the site a
   `service` device? A bank is neither a physical control unit nor a service
   (device registry definition). Where do bank-level entities live?
2. **`unique_id` scheme.** Inverters use the serial (Solis 33004–33019). The
   AlphaESS serial source is unidentified locally. The gateway has no Modbus
   identity, and IP or hostname are disallowed. The site and banks need a
   last-resort config-entry id plus a stable suffix. Decide the format, and
   what happens when an inverter is replaced.
3. **Config-entry topology.** One site entry holding every bus, or one entry
   per bus plus a site or add-on-link entry? What is the entry's `unique_id`?
   It must not be the discovery uuid on a `hassio`-source entry, or removing
   the add-on deletes the entry and its authority store (Core
   `hassio/discovery.py:157-160`). That would break C1 and the removal handover
   at `0008:77`. How does a standalone-adopted entry (#257 step 2) later link
   to a discovered add-on?
4. **Plan sensors.** Does the add-on stop REST `set_state` at cutover, with the
   integration creating the plan sensors from data fetched over the channel?
   Which of today's 14 survive? `proactive_mode` is retired (`0008:13`), and
   the `fallback_*` attributes become integration-owned. What do they show when
   the add-on is down: unavailable (Silver `entity-unavailable`) or the last
   value?
5. **Entity-id continuity.** `has_entity_name` derives ids from device names,
   so today's `sensor.ha_spark_*` ids change. Is a fresh install (#253) enough,
   or do household dashboards and automations need preserved ids
   (`suggested_object_id`, or a one-time rename note)?
6. **Write entity shape per control.** Should slot windows be per-field `time`
   entities or one compound control? The 8-register block is a single commit
   (`solis.py:15-18`), so per-field writes create half-written windows. Should
   currents be in amps or watts? Which of the ~45 registers get entities in v1,
   and which wait for parity?
7. **Assist and LLM exposure of write-capable entities.** The settled text bars
   only mode and manual state from Assist, by making them sensors (`0008:59`).
   It says nothing about device controls, and in Observe "any Home Assistant
   caller" may write (`0008:61`). Options include `entity_category: config`,
   which suppresses default exposure (`exposed_entities.py:309-312`), an
   explicit un-expose, or avoiding the `switch` platform, which is
   default-exposed (`:32-44`). Also decide whether HA's own conversation agent
   writing a device entity is acceptable under "the LLM never controls
   hardware" (CLAUDE.md, which is written about ha-spark's LLM).
8. **Owner service set.** Which owner services exist: only → Observe and manual
   start or resume, or also → Protect? What are their names and parameters,
   their error and translation shape, and their audit entry? Is inverter clock
   sync (`solis sync-clock`) an owner service, an add-on command, a `button`
   entity, or integration-internal?
9. **"Verified" in the integration.** Define the read-back count, timing and
   zero confirmation for direct Modbus reads. Today's constants were tuned for
   an HA overlay (`solis.py:100-111`). Is caching checked per write, or only at
   probe and Re-probe? Define the result vocabulary posted back per sequence
   (today `[APPLIED]`, `[WARNING]`, `[FAILED]`, `[SKIP]`, `[BLOCKED]`).
10. **Plan-apply command granularity.** Does the add-on send per-device intents
    (window, target SoC, current) or register-level setpoints? The integration
    clamps (C16) and validates limits but makes no optimisation decisions
    (`0005:56-60`). Who owns write-if-changed and the export-window arming rules
    (`solis.py:20-27`)?
11. **How the add-on reads integration-owned state.** The mode, epoch, holds,
    guard allowance, major-load demand and Solcast data could arrive as HA
    entity states over REST, or over the authenticated channel. HA states are
    overwritable by any REST caller (`POST /api/states`), so authority-relevant
    reads over REST would trust a forgeable value.
12. **Override sensor feed.** The override lives in the add-on's store (C12).
    How does it reach the integration's sensor, and what does the sensor show
    while the add-on is down or the override is suspended?
13. **Owner of the dispatch and EV evidence entities.** Setup's Tariff step
    confirms the Octopus dispatch entities (#257 step 5), and the EV step
    confirms the EV entities. The integration's protection needs both
    (`0008:75`), and each Settings section saves to one store (#306). Which
    store holds those entity ids and the per-source freshness limits (#285
    proposals)?
14. **Solcast delivery and key.** Should the forecast be entity attributes
    (48 × 3 slots fits under the 16 KiB recorder limit, but is chatty) or
    channel data? Confirm that the key lives in the integration's config entry
    (`0006:20`, since Solcast is integration I/O). How is the
    one-poll-before-the-evening-plan timing coordinated with the add-on's plan
    time (`solcast-api.md:195-201`)?
15. **Exposure of safeguard state.** Which supply-guard, tripwire, hold and
    freshness values become entities, which are diagnostics only, and which are
    disabled by default? Does the planner consume the live allowance (#241:
    "planner and drivers share effective limits")?
16. **Notifications.** The integration notifies about fallback (`0005:52`) and
    removal (`0008:77`). The add-on notifies about overrides (`0009:42`) and the
    digest (#306 Notifications section). Who owns the notify target setting, and
    does the integration use HA notify, persistent notifications or repairs?
17. **Quality-scale target and redaction list.** Which tier should v1 aim for?
    Gold covers devices, diagnostics, repairs, reconfiguration and translations.
    The diagnostics `TO_REDACT` set should cover the discovery token, the
    Solcast key, the gateway host, inverter serials and the household's entity
    ids.
18. **Behaviour on version skew and lost authority.** #252 recommended keeping
    entities read-only on `api_version` skew, which conflicts with C1 and C10
    (manual writes in Observe). Under *authority unknown* (C11), are device
    entities unavailable, or present but refusing writes with a service error?

### Stale wording to flag (not a decision)

These lines predate #291. They say add-on commands travel as integration
services. Amending them is a wording fix:

- `0005:12-13`: "The integration exposes services for add-on commands".
- `0005:90` and `0005:99-100`: "HA services and entities remain the
  add-on-to-integration boundary".
- The map #247 Notes: the add-on "actuates through services the integration
  registers".

The #252 write-up's `:8099` security finding has partly moved on:
`ha_spark/api/server.py:8-14` now describes an ingress peer check. Its
replacement is settled at `0008:48`.

## Sources

- Decision comments: [#165](https://github.com/Kylevdm/ha-spark/issues/165#issuecomment-6118621456), [#248](https://github.com/Kylevdm/ha-spark/issues/248#issuecomment-6103031446), [#249](https://github.com/Kylevdm/ha-spark/issues/249#issuecomment-6059216761), [#251](https://github.com/Kylevdm/ha-spark/issues/251#issuecomment-6059172965), [#252](https://github.com/Kylevdm/ha-spark/issues/252#issuecomment-6059187751), [#253](https://github.com/Kylevdm/ha-spark/issues/253#issuecomment-6103174886), [#255](https://github.com/Kylevdm/ha-spark/issues/255#issuecomment-6131841097), [#257](https://github.com/Kylevdm/ha-spark/issues/257#issuecomment-6132772796), [#259](https://github.com/Kylevdm/ha-spark/issues/259#issuecomment-6114016256), [#266](https://github.com/Kylevdm/ha-spark/issues/266#issuecomment-6125619923), [#267](https://github.com/Kylevdm/ha-spark/issues/267#issuecomment-6119562753), [#268](https://github.com/Kylevdm/ha-spark/issues/268#issuecomment-6111196558), [#269](https://github.com/Kylevdm/ha-spark/issues/269#issuecomment-6132918848), [#278](https://github.com/Kylevdm/ha-spark/issues/278#issuecomment-6133071314), [#283](https://github.com/Kylevdm/ha-spark/issues/283#issuecomment-6133231536), [#285](https://github.com/Kylevdm/ha-spark/issues/285#issuecomment-6125943045), [#291](https://github.com/Kylevdm/ha-spark/issues/291#issuecomment-6142935741), [#296](https://github.com/Kylevdm/ha-spark/issues/296#issuecomment-6142221487), [#297](https://github.com/Kylevdm/ha-spark/issues/297#issuecomment-6145615838), [#306](https://github.com/Kylevdm/ha-spark/issues/306#issuecomment-6177623171).
- Repo: `docs/adr/0005`–`0009`, `CONTEXT.md`, `docs/research/hacs-addon-integration-packaging.md`, `docs/research/solis-modbus-register-map.md`, `docs/research/alphaess-local-modbus.md`, `docs/research/solcast-api.md`, `ha_spark/energy/publish.py`, `ha_spark/energy/v2l.py`, `ha_spark/ha/rest.py`, `ha_spark/devices/inverters/solis.py`, `ha_spark/devices/inverters/alphaess.py`, `ha_spark/api/server.py`, `ha_spark/energy/scheduler.py`.
- HA developer docs: [Entity](https://developers.home-assistant.io/docs/core/entity/), [Entity registry](https://developers.home-assistant.io/docs/entity_registry_index), [Device registry](https://developers.home-assistant.io/docs/device_registry_index), [Diagnostics](https://developers.home-assistant.io/docs/core/integration_diagnostics), [Quality scale rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/), [action-setup](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup), [REST API](https://developers.home-assistant.io/docs/api/rest/).
- HA Core @`1e76a638`: [`homeassistant/exposed_entities.py`](https://github.com/home-assistant/core/blob/1e76a638e0a73e689116e20452d803f74c9bf9c9/homeassistant/components/homeassistant/exposed_entities.py), [`hassio/discovery.py`](https://github.com/home-assistant/core/blob/1e76a638e0a73e689116e20452d803f74c9bf9c9/homeassistant/components/hassio/discovery.py), [`recorder/db_schema.py`](https://github.com/home-assistant/core/blob/1e76a638e0a73e689116e20452d803f74c9bf9c9/homeassistant/components/recorder/db_schema.py).
