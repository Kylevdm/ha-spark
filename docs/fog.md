# Fog ledger

Fog is the dim view ahead of an active map: in-scope areas you can tell are coming but cannot yet phrase sharply enough to ticket. The map's **Not yet specified** section is the store for active-map fog. This ledger is the live index across maps and sessions.

Last swept: Never.

## Where to put things

| It is… | It goes… |
| --- | --- |
| Unsharp and in scope for an active map | That map's **Not yet specified** |
| Sharp enough to state as a question | A ticket on its map, even if blocked |
| Already decided | The map's **Decisions so far**, linking the resolution |
| Past one map's destination but wanted and owned by no current map | **Deferred efforts** |
| Conditional, within a closing map's destination, unowned, and not triggered | Mark **CARRIED** on the map and index it under **Carried** |
| A random wanted idea with no source map or owner | **Deferred efforts** |

Closing a map requires marking every patch in **Not yet specified**:

- **ANSWERED** — current evidence resolved it; state and link the answer.
- **REHOMED** — another map, ticket, or scope owns it; link the owner.
- **CARRIED** — still within the destination, still unowned, and its trigger did not fire; preserve the human's rationale and index it below.

A patch that blocks the destination prevents closure. Work explicitly beyond the destination is never Carried.

Every live row states what it is, cites where it touches the build when applicable, and names the observable event that makes it ready for owned work. `Trigger: none yet` is valid.

## Carried

Live conditional fog from closed maps, grouped by the map that raised it.

<!--
### From [Map title](map link) (closed)

| Patch | Trigger |
| --- | --- |
| What remains unknown, with evidence such as `path:line` or a linked issue | Observable trigger, or none yet |
-->

## Triaged

Closed maps whose **Not yet specified** patches are all marked. Record maps with zero patches too.

<!-- - [Map title](map link) — triaged YYYY-MM-DD: 0 ANSWERED, 0 REHOMED, 0 CARRIED. -->

## Deferred efforts

Wanted work outside every current map's destination and owned by nobody. An idea that never came from a map belongs here too.

<!--
- **What it is.** Where it already touches the build, with evidence, when applicable.
  Trigger: none yet.
-->

- **[P1] Make live-tariff schedules independent of load-slot forecasts.** Both live providers fall back to fixed pricing when `load_slots` is absent, so the daily-model forecast silently disables valid rate feeds (`ha_spark/energy/tariff.py:176`, `ha_spark/energy/tariff.py:218`, `ha_spark/energy/sources.py:299`; follow-up to [#34](https://github.com/Kylevdm/ha-spark/issues/34)).
  Trigger: none yet.

- **[P1] Make the charger use the dynamically selected cheap slots.** Dynamic selection changes `cheap_fracs`, but neither adds the slots to `controlled_windows` nor changes the fixed `ChargeIntent` window, so planning and physical charge timing diverge (`ha_spark/energy/tariff.py:186`, `ha_spark/energy/planner.py:149`; follow-up to [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P1] Fall back on any missing dynamic-tariff slot.** Partial coverage currently substitutes the standard rate and still selects slots, allowing an incomplete feed to influence actuation instead of degrading safely (`ha_spark/energy/tariff.py:180`, `ha_spark/energy/tariff.py:183`; follow-up to [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P1] Cost planned imports from their actual schedule slots.** Baseline cost uses `schedule.prices`, while planned charge purchases still use representative scalar rates, making variable-tariff projections and savings inconsistent (`ha_spark/energy/planner.py:96`, `ha_spark/energy/planner.py:135`; follow-up to [#36](https://github.com/Kylevdm/ha-spark/issues/36)).
  Trigger: none yet.

- **[P2] Require or build the configured schedule at every planner call.** The agent plan/forecast endpoints and offline intent parser call `compute_plan` without a schedule and therefore silently get a fixed schedule under live-provider configs (`ha_spark/agent/tools.py:59`, `ha_spark/agent/tools.py:82`, `ha_spark/intent_parser.py:89`; follow-up to [#36](https://github.com/Kylevdm/ha-spark/issues/36)).
  Trigger: none yet.

- **[P2] Rate backtest intervals from per-slot schedule prices.** Backtesting still buckets imports by the fixed clock window and computes totals from `cheap_rate`/`standard_rate`; `schedule.prices` is never consumed (`ha_spark/energy/backtest.py:48`, `ha_spark/energy/backtest.py:70`, `ha_spark/energy/backtest.py:79`; follow-up to [#37](https://github.com/Kylevdm/ha-spark/issues/37)).
  Trigger: none yet.

- **[P2] Build the configured provider schedule in the backtest command.** The CLI always constructs a legacy scalar `TariffSchedule`, so dynamic and Octopus configurations cannot affect `ha-spark backtest` (`ha_spark/cli.py:291`, `ha_spark/cli.py:298`; follow-up to [#37](https://github.com/Kylevdm/ha-spark/issues/37)).
  Trigger: none yet.

- **[P1] Convert Octopus unit rates from pence to GBP at the provider boundary.** `value_inc_vat` is stored unchanged in `PricePoint.price`, making Octopus costs 100 times too large relative to the planner's GBP/kWh contract (`ha_spark/energy/octopus.py:139`, `ha_spark/energy/octopus.py:149`; follow-up to [#39](https://github.com/Kylevdm/ha-spark/issues/39)).
  Trigger: none yet.

- **[P1] Reject non-array dynamic-rate payloads safely.** `_parse_price_points` iterates `raw or []` without checking its container type, so a truthy scalar can raise before fallback handling (`ha_spark/energy/sources.py:82`, `ha_spark/energy/sources.py:85`; follow-up to [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P1] Reject non-finite dynamic prices.** `_opt_float` accepts `nan`/`inf` and the parser constructs a `PricePoint`, allowing non-finite values into sorting and cost arithmetic (`ha_spark/energy/sources.py:93`, `ha_spark/energy/sources.py:96`; follow-up to [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P1] Turn malformed top-level Octopus JSON into provider failure.** A successful response whose JSON is not an object reaches `payload.get` outside the caught parse block and can crash the daemon or health check instead of falling back (`ha_spark/energy/octopus.py:183`, `ha_spark/energy/octopus.py:190`; follow-up to [#39](https://github.com/Kylevdm/ha-spark/issues/39)).
  Trigger: none yet.

- **[P2] Restrict Octopus rate pagination to the configured origin.** The authenticated client follows an unvalidated absolute `next` URL, which can send Basic Auth credentials and traffic to another host (`ha_spark/energy/octopus.py:179`, `ha_spark/energy/octopus.py:185`, `ha_spark/energy/octopus.py:191`; follow-up to [#39](https://github.com/Kylevdm/ha-spark/issues/39)).
  Trigger: none yet.

- **[P2] Parse and validate dynamic rates before reporting health.** Health counts list entries without applying provider validation, so malformed timestamps or empty objects can report OK even though planning obtains no usable points (`ha_spark/health.py:214`, `ha_spark/health.py:223`; follow-up to [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P2] Warn when Octopus returns no complete usable rate horizon.** A successful empty or incomplete rates response reports OK even though planning falls back to fixed pricing (`ha_spark/health.py:240`, `ha_spark/health.py:253`; follow-up to [#39](https://github.com/Kylevdm/ha-spark/issues/39)).
  Trigger: none yet.

- **[P2] Report the selected dynamic slot indices and prices.** The report counts only slots equal to the absolute minimum and renders a min–max range, which can misstate how many varying-price slots were selected and hides the actual choice (`ha_spark/energy/report.py:36`, `ha_spark/energy/report.py:38`; follow-up to [#36](https://github.com/Kylevdm/ha-spark/issues/36) and [#38](https://github.com/Kylevdm/ha-spark/issues/38)).
  Trigger: none yet.

- **[P2] Auto-discover Octopus product/tariff codes from the account API.** `octopus_product_code` and `octopus_tariff_code` are manually configured but can be fetched from `/v1/accounts/{account_number}/` using the API key — the active agreement contains both. Would reduce Octopus config to just API key + account number (`ha_spark/energy/octopus.py:161`, `ha_spark/config.py:199`).
  Trigger: none yet.
