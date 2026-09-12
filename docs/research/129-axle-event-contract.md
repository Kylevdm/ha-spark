# Research: the Axle export-event API and the Home Assistant fallback contract

Issue: [#129](https://github.com/Kylevdm/ha-spark/issues/129), part of map
[#128](https://github.com/Kylevdm/ha-spark/issues/128).
Date: 2026-09-12. All sources fetched read-only; no credentials read, printed,
copied, or committed, and no Home Assistant writes made.

## Question

What exact, current, first-party contract can ha-spark rely on to discover Axle
export events: authentication and access, event identity/status, exact start and
end times, direction, rate, updates/cancellation, polling constraints,
malformed/unavailable behaviour, and the corresponding exact event fields
exposed by the installed Home Assistant Axle integration as a temporary
fallback?

## Verdict

There are **three distinct Axle surfaces**, and only one of them is a plausible
per-user route for ha-spark:

| Surface | Contract owner | Usable by a single household? | What it gives |
| --- | --- | --- | --- |
| `vpp:dispatch:requested` webhook | Axle, documented in developer docs | **No — OEM/partner pathway** (webhook registered by Axle via email) | Event id, exact UTC window, per-asset target `power_kw` |
| Site/asset REST API (`flex-events`, `price-curve`, `todays-dispatch-schedule`, …) | Axle, documented in developer docs + OpenAPI | **Unknown — requires org/partner credentials and (for a plan) an onboarded asset with consent** | Past-event settlement, half-hourly flex prices, and (production only) today's battery plan |
| `GET /vpp/home-assistant/event` (the HA fallback) | Axle, documented only on the marketing landing page as a beta | **Yes — static token from the account's Home Assistant section** | Next event's `start_time`, `end_time`, `import_export`, `updated_at` |

The practical finding for the prototype:

- **Verified first-party:** the HA fallback endpoint's shape, auth, polling
  hint, and its four fields. It gives **absolute UTC-aware start/end times and an
  explicit `"import"`/`"export"` direction** — enough to schedule a window and
  verify direction — but it carries **no event id, no status, and no rate**.
- **Verified first-party:** the VPP dispatch webhook's full event contract
  (identity, UTC window, signed rate in kW, retry semantics). It is the only
  surface that carries a rate, and it is not available to a household on the
  documented path.
- **Unknown / undocumented:** any Axle **cancellation or change event** on the
  HA endpoint. The string "cancel" appears nowhere in the Axle docs corpus; the
  only occurrence in the whole API surface is the unrelated
  `PaymentStatus.cancelled` enum. The only observable signals are
  `updated_at` changing, the times/direction changing, or the response becoming
  empty — and the documented contract does not distinguish those from "no event
  yet", "event ended", or a failed fetch.
- **Not Axle fields:** the HA integration's `event_window_state`,
  `event_in_progress`, and `event_minutes_to_start` are **locally derived** from
  `start_time`/`end_time` against HA's clock. They are not Axle status, and a
  relative countdown must never be used to reconstruct an absolute event time.

## Sources and method

Tier 1 — Axle first-party:

1. Axle developer docs (Mintlify), including the per-page `.md` sources and the
   `llms-full.txt` corpus: <https://docs.axle.energy/> (accessed 2026-09-12).
   - VPP overview: <https://docs.axle.energy/workflows/axle-vpp/overview>
   - VPP integration: <https://docs.axle.energy/workflows/axle-vpp/integration>
   - Dispatch webhook: <https://docs.axle.energy/workflows/axle-vpp/api-reference/dispatch>
   - Webhooks (envelope, signature, retries): <https://docs.axle.energy/workflows/webhooks>
   - Auth (`/auth/token-form`): <https://docs.axle.energy/api-reference/auth/token-form>
   - Sandbox/production: <https://docs.axle.energy/api-reference/sandbox>
   - Get site flex events: <https://docs.axle.energy/api-reference/entities/site/flex-events>
   - Get site price curve: <https://docs.axle.energy/api-reference/entities/site/price-curve>
2. Axle live OpenAPI document, version **1.4.6**:
   <https://api.axle.energy/openapi.json> (accessed 2026-09-12). This is the
   authoritative path list and schema set; the docs pages embed a copy.
3. Axle Home Assistant landing page (the only first-party page that documents
   the HA endpoint):
   <https://vpp.axle.energy/landing/home-assistant> (accessed 2026-09-12).

Tier 2 — integration source and repo evidence:

4. Community HACS integration `deanhalllincoln/ha-axle-vpp`, tag `v1.0.13`
   (latest release, 2026-09-03), read at tag:
   <https://github.com/deanhalllincoln/ha-axle-vpp>. Axle's own landing page
   calls this "a community-built integration by Dean Hall" and links to it; it
   is **not** a first-party Axle artifact.
5. PredBat's Axle provider (`axle.py`), which Axle's landing page endorses as
   "Axle VPP is built in":
   <https://github.com/springfall2008/batpred/blob/master/apps/predbat/axle.py>.
6. Read-only repository evidence from this repo: the 2026-09-07 observation
   (`docs/runbooks/RUN-83-log.md`, `axle_observer.py`, `axle-83.csv`), which
   records the event's real 19:00–20:00 BST window and the installed entity ids.

Tier 3 — independent client (corroborating, not authority):

7. `Herbertmt978/python-axle` (`aioaxlevpp`), an independent client for the same
   endpoint: <https://github.com/Herbertmt978/python-axle>. It documents its
   own contract source as the landing page above, adds strict validation, and
   reports one field (`opted_out`) it says it verified "using a development
   account" — that field is **not** in any first-party source and is flagged as
   third-party-observed below.

## 1. Authentication and access

### 1.1 The per-user Home Assistant token (the fallback's auth)

The landing page is explicit ([source](https://vpp.axle.energy/landing/home-assistant)):

> Log in to your Axle account and go to Account Settings. Navigate to the Home
> Assistant section (**only available when using Events Only mode**). Click
> Generate Token … Keep your token secure.

It is used as a static bearer token against the HA endpoint, and the page's
worked `configuration.yaml` example sets `scan_interval: 600` (10 minutes) and
lists `json_attributes: start_time, end_time, import_export, updated_at`. The
page marks the whole feature **Beta**: "These fields are subject to change. We
may add or remove attributes in the future and will notify you by email before
making any breaking changes."

Note the token is a long-lived per-account token, not the 1-hour bearer token
that `POST /auth/token-form` returns (see 1.2). The community integration stores
it as a config-entry token; the manual route stores it in `configuration.yaml`.

### 1.2 The organisational/partner API token

`POST /auth/token-form` exchanges **username + password** for an
**organisation-scoped** bearer token valid **1 hour** ([docs](https://docs.axle.energy/api-reference/auth/token-form)).
Sandbox is `https://api-sandbox.axle.energy`, and the docs say to "get in touch"
for a sandbox or production account ([sandbox](https://docs.axle.energy/api-reference/sandbox)).
The live OpenAPI is served from `https://api.axle.energy` (production), version
1.4.6. This is the credential class the documented site/asset endpoints expect;
whether a single household has one is not established by any source here.

### 1.3 The VPP dispatch webhook (OEM-only on the documented path)

The VPP integration page opens with ([source](https://docs.axle.energy/workflows/axle-vpp/integration)):

> This pathway is for battery manufacturers (OEMs) who run their own cloud
> platform … We'll send dispatch instructions to an endpoint you provide.

The webhook URL is registered by contacting Axle ("You supply the webhook URL
when you set up your integration — get in touch to register it"). There is no
self-serve per-household webhook in any documented surface.

### 1.4 What is deliberately not in the public API

The live OpenAPI contains **no** path matching `home` or `vpp` (verified by
enumerating `paths` from <https://api.axle.energy/openapi.json>); in particular
`GET /vpp/home-assistant/event` is absent. The HA endpoint is documented only on
the marketing landing page, not in the versioned developer docs. The developer
docs corpus (`llms-full.txt`) contains **no** occurrence of "Home Assistant".

## 2. The official VPP dispatch event — `vpp:dispatch:requested`

This is the richest first-party event contract, but it arrives by push webhook
([webhooks](https://docs.axle.energy/workflows/webhooks), [dispatch](https://docs.axle.energy/workflows/axle-vpp/api-reference/dispatch)).

Standard envelope:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | UUID | Delivery id; stable across retries; **dedupe on this** |
| `version` | string | `v1.0.0` for `vpp:` events |
| `event_type` | string | `vpp:dispatch:requested` |
| `created_at` | ISO 8601 UTC | When Axle recorded it |
| `payload` | object | Event-specific fields below |

`payload` for `vpp:dispatch:requested`:

| Field | Type | Meaning |
| --- | --- | --- |
| `event_id` | UUID | **Grid event identity**, shared by every asset in the instruction; distinct from envelope `id` |
| `issued_timestamp` | ISO 8601 UTC | When Axle issued the instruction |
| `start_time` | ISO 8601 UTC | Start of the dispatch window |
| `end_time` | ISO 8601 UTC | End of the dispatch window |
| `assets[]` | array | One entry per affected asset |
| `assets[].asset_id` | UUID | Axle asset identifier |
| `assets[].power_kw` | number | Target power at the inverter; **positive = charge/import, negative = discharge/export** |

Notice and semantics ([integration](https://docs.axle.energy/workflows/axle-vpp/integration)):

- "We give you at least **30 minutes' notice** before an event (normally 4 hours
  to 1 day ahead) via the dispatch webhook. We do not currently run
  very-short-notice (sub-30-minute) events."
- "The event is sent when the dispatch is scheduled, so don't act on it until
  `start_time`."
- `power_kw` "applies at the **inverter**", and Axle "normally dispatch[es] at
  the inverter's maximum power rating", accounting for household load.
- "Customers should be able to override dispatch instructions at any time" and
  "The battery should return to its original mode of operation after the
  dispatch event has finished."

Delivery contract ([webhooks](https://docs.axle.energy/workflows/webhooks)):
at-least-once and unordered; dedupe on the envelope `id`; sequence on
`created_at` where order matters; a `2xx` durably accepts, `4xx` is a definitive
refusal (never retried), `5xx`/timeout retries with exponential backoff (30 s
doubling to a 4 h cap, ±10% jitter) and **retries stop 24 h after `created_at`**.
The instruction is batched: a `4xx` refuses the whole event; an unrecognised
`asset_id` should be skipped while still returning `2xx`. Requests carry
`x-axle-sig` (`t=<unix>,v1=<hex HMAC-SHA256>` over `"{t}.{body}"`, 300 s replay
tolerance) and an unsigned `x-axle-ref`.

**Cancellation/change:** the event table lists exactly four webhook types
(`user:onboarding:complete`, `asset:dispatch:requested`, `vpp:asset:registered`,
`vpp:dispatch:requested`). There is **no cancellation or update event type** in
any first-party source, and the string "cancel" does not appear in the docs
corpus. A scheduled dispatch is therefore one immutable instruction as far as
the published contract goes.

### 2.1 Do not confuse this with the asset dispatch API

The OpenAPI separately defines a `DispatchEvent` schema with `from_time`/
`to_time`, `level_kw`, `market_id`, and `update_status_callback`, plus an
`EventStatus` enum (`RECEIVED`, `ACCEPTED`, `REJECTED`, `FAILED`, `EXECUTED`) and
`POST /entities/asset/{asset_id}/callback/{event_id}`. Those belong to the
general asset-level dispatch/status API (the same OpenAPI that also carries EV
`charge_now`/`charge_now_deleted` events), **not** to the VPP webhook. The
`vpp:dispatch:requested` payload uses `start_time`/`end_time`/`assets[].power_kw`
and has no status field; the two field sets are not interchangeable.

## 3. The documented site/asset API surfaces

These are documented and appear in the OpenAPI, but their applicability to a
single household account is unknown (see 3.4).

### 3.1 `GET /entities/site/{site_id}/flex-events`

Returns events the site **has participated in**, with `start_at`, `end_at`,
`estimated_gross_revenue_gbp`, and optional `final_gross_revenue_gbp`
([docs](https://docs.axle.energy/api-reference/entities/site/flex-events)). This
is **post-hoc settlement**, not an upcoming-event feed: it cannot schedule an
event ahead of time.

### 3.2 `GET /entities/site/{site_id}/price-curve`

Half-hourly `start_timestamp` + `price_gbp_per_mwh`, for the current settlement
period through 23:00 UK on the same or next day; next-day prices appear from
**14:00 UK**. "If the asset is not participating in the market for that period,
`price_gbp_per_mwh` will be None" ([docs](https://docs.axle.energy/api-reference/entities/site/price-curve)).

### 3.3 `GET /entities/asset/{asset_id}/todays-dispatch-schedule`

The closest thing to an upcoming-window feed: today's ordered periods, each with
`start_timestamp`, `end_timestamp`, and `action` ∈ `CHARGE`, `DISCHARGE`,
`LOCK`, `FEED_IN`, `SELF_USE`, `PAUSE` (UK time), per the live OpenAPI. It is
**production-only** (the sandbox always returns `{"periods": []}`), and an empty
plan is also returned when the battery "is not optimised (no asset model
recorded, or no full schedule control or wholesale market consent)" — the
response "does not distinguish between these". `GET /entities/asset/{asset_id}/battery-schedule`
similarly returns `schedule_steps` with `charge_max`/`discharge_max`/
`avoid_export`/`avoid_import` and `schedule_last_updated`.

### 3.4 Access is the open question

Access to these endpoints requires organisation-scoped credentials and, for the
schedule, an onboarded asset with schedule/wholesale consent. Nothing in the
sources establishes that Kyle's Events-Only account holds either. This is the
one access question the research could not close without touching account
credentials, and it is recorded as a narrow follow-up (see §9).

## 4. The Home Assistant fallback contract

### 4.1 First-party field contract

Endpoint: `GET https://api.axle.energy/vpp/home-assistant/event`
(`Authorization: Bearer <token>`, `Accept: application/json`), from the landing
page's worked example and attribute table:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `start_time` | ISO 8601 datetime | "When the next grid event starts" |
| `end_time` | ISO 8601 datetime | "When the grid event ends" |
| `import_export` | string: `"import"` \| `"export"` | "Whether to import from or export to the grid" |
| `updated_at` | ISO 8601 datetime | "When the event data was last updated" |

The endpoint returns the **next (single) event**, not a list. "When no event is
scheduled, the sensor state will be empty." The shape is **beta and subject to
change**. `event_id`, status, rate, and cancellation are **not** in this
contract.

### 4.2 Polling

The landing page's example uses `scan_interval: 600` (10 minutes) and states
"This creates a REST sensor that polls the Axle API every 10 minutes for
upcoming grid events." No rate limit is documented. The community integration
polls at 600 s normally and 60 s from 2 hours before `start_time` through
`end_time` (`coordinator.py`). Detection latency for a newly published or
changed event is therefore up to one poll interval (~10 min idle, ~1 min near
the event). The "≥30 min notice" statement belongs to the **webhook** pathway
(§2); no first-party source states a minimum notice specifically for the HA
endpoint.

### 4.3 What is source data vs derived

Only the four fields above come from Axle. In the community integration
(`sensor.py`, `coordinator.py`), everything else is computed locally from
`start_time`/`end_time` against HA's clock:

- `event_window_state` → `in_progress` / `upcoming` / `finished`
  (`start <= now <= end`, else before/after).
- `event_in_progress`, `event_1_hour_before`, `event_2_hours_before`,
  `event_tomorrow`, `event_later_today`, `event_completed_today` — time/date
  comparisons.
- `event_minutes_to_start`, `event_remaining_minutes` — countdowns refreshed
  each minute.

This matters for the prototype: those derived sensors are **not** Axle status,
and `event_minutes_to_start` is a **relative countdown** — the 2026-09-07
observation used it only to sanity-check the absolute `start_time`, and event
times must be taken solely from `start_time`/`end_time`. The observation also
showed the inverter's own RTC fired ~15 s early while the integration's locally
computed `event_window_state` flipped ~9 s **after** the physical edge
(`docs/runbooks/RUN-83-log.md`) — local derivation, not provider truth.

### 4.4 Installed entity ids

Repo evidence from the 2026-09-07 observation (read-only `axle_observer.py`
against the live site) records these entity ids actually present:

- `sensor.axle_vpp_axle_event_window_state`
- `sensor.axle_vpp_axle_event_minutes_to_start`
- `sensor.axle_vpp_axle_import_export`
- `sensor.axle_vpp_axle_event_in_progress` (recorded `on`/`off`)

The upstream `v1.0.13` source declares the same logical entities; `start_time`,
`end_time`, `import_export`, `updated_at`, `event_minutes_to_start`,
`event_remaining_minutes`, `event_window_state` are `SensorEntity`s and the
window flags are declared `BinarySensorEntity` in `sensor.py`. The installed
integration presents `event_in_progress` under the **`sensor.`** domain, so
before coding against it, confirm the installed integration version and the
entity domains live — the observed id is the safe one to use. The community
integration is third-party; the first-party artifact is the REST field contract
in 4.1, and a fallback implementation should read `start_time`/`end_time`/
`import_export`/`updated_at` rather than depend on the derived sensors.

The integration also creates `calendar.axle_next_event` (summary
`Axle <Import|Export> Event`, extra attributes `import_export`, `updated_at`),
from `calendar.py`.

## 5. Updates, cancellation, and failure behaviour

- **Updates.** `updated_at` is the only first-party signal that Axle changed the
  event data. The landing page documents no update cadence and no diff
  semantics. A changed window should be detected by comparing the tuple of
  absolute fields, not the countdown.
- **Cancellation.** Not documented anywhere. No webhook event type, no
  `status` field, no "cancelled" value for the HA endpoint. The only
  first-party statement is "When no event is scheduled, the sensor state will be
  empty", which is indistinguishable from "already finished" or "nothing
  published yet".
- **Provider failure cannot extend a schedule.** Nothing in either contract
  emits a later "end" for an existing event; the HA endpoint only ever returns a
  single next event. This is consistent with the map's rule that the **last
  verified event end remains a hard stop**. That is an inference from the
  documented shape, not a documented cancellation guarantee, and should be
  treated as a safe default to verify against a real change/cancellation.
- **Malformed / unavailable (documented contract).** Axle documents nothing
  about the HA endpoint's error bodies. The community integration's behaviour
  (not Axle's contract): a `200` whose body lacks `start_time` is treated as
  "no event" (`None`); a non-200 raises and the coordinator marks entities
  unavailable; malformed timestamps make the derived sensors return `None`/
  `False`. The independent `aioaxlevpp` client instead rejects malformed bodies
  as errors so that "malformed data must not look like a cancelled event"
  (its own test name) — a hardening stance ha-spark should adopt: **only an
  empty/`null` response means "no event"; anything malformed is a failure, not
  a cancellation.** It also treats `401`/`403` as auth failure, uses a 10 s
  timeout, and does not follow redirects.
- **Third-party-only field.** `aioaxlevpp` documents an `opted_out` boolean it
  says it verified against a development account, "Preserve the explicit
  participation flag returned by the live API." It is **not** in any Axle
  source. Do not depend on it without first-party confirmation.

## 6. Gaps against the prototype's needs

| Prototype need | HA fallback | Official webhook |
| --- | --- | --- |
| Absolute start/end in UTC | ✅ `start_time`/`end_time` | ✅ `start_time`/`end_time` |
| Direction (export vs import) | ✅ `import_export == "export"` | ✅ `power_kw < 0` |
| Export rate | ❌ none — must be configured/assumed | ✅ `power_kw` per asset |
| Event identity for dedupe | ❌ no `event_id`; identity is the window tuple | ✅ `event_id` + envelope `id` |
| Status | ❌ none (derived locally) | ❌ none on this event |
| Change/cancellation signal | ⚠️ only `updated_at`/fields/emptiness | ❌ none documented |
| Delivery | poll, ≤10 min idle latency | push, ≥30 min notice |
| Access for one household | ✅ static token | ❌ OEM/webhook registration |

The map's planner needs a **price** for event slots, not a setpoint, so the
missing `power_kw` in the fallback is not a blocker by itself; but the rate is
not discoverable from the fallback and must come from configuration (PredBat
does exactly this with an `axle_pence_per_kwh` setting defaulting to 100).

## 7. What is verified vs not

**Verified first-party:**

- HA endpoint URL, bearer auth, static Events-Only token, `scan_interval: 600`,
  the four fields and their types, the `"import"`/`"export"` values, the
  "empty when no event" statement, and the beta caveat.
- The full `vpp:dispatch:requested` payload, envelope, HMAC signature, retry and
  at-least-once semantics, ≥30 min notice, `power_kw` sign convention, and the
  absence of any cancellation/update event type.
- `flex-events`, `price-curve`, and the schedule endpoints' schemas.
- The HA endpoint is absent from the live OpenAPI.

**Inaccessible / not established:**

- Any per-household route to the VPP webhook or the org-scoped site/asset API.
- Whether the HA token authenticates against any documented endpoint other than
  `/vpp/home-assistant/event`.

**Unknown / undocumented:**

- Event update and cancellation semantics on the HA endpoint.
- Whether `updated_at` changes for a modification, and whether the event ever
  disappears before its `end_time`.
- The exact installed integration version/domains (repo evidence gives the ids
  actually observed; upstream source is third-party).
- The `opted_out` field (third-party report only).

## 8. Suggested fallback reading rules (for [#130](https://github.com/Kylevdm/ha-spark/issues/130))

Not a decision — the source-contract choice belongs to
[Choose the event-source contract for the supervised Axle prototype](https://github.com/Kylevdm/ha-spark/issues/130).
The evidence supports these conservative defaults:

1. Only ever trust `start_time` / `end_time` / `import_export`; never derive an
   absolute time from a countdown.
2. Filter to `import_export == "export"`; treat any other value as not-ours.
3. Treat "no event" only as an empty/`null`/no-`start_time` response; treat
   malformed payloads and non-200s as provider failure, never as cancellation.
4. Detect change/cancellation by comparing the absolute window tuple and
   `updated_at`; on any disagreement, keep the **last verified end** as the hard
   stop rather than extending it.
5. Configure the event rate rather than expecting it from the fallback.
6. Do not depend on the integration's derived `event_window_state` /
   `event_minutes_to_start` for scheduling.

## 9. Open follow-ups

The one sharply phrased, answerable question this research leaves is whether the
official documented API is reachable for this account at all (see §3.4): if
Kyle's Events-Only account can hold partner credentials or reach
`todays-dispatch-schedule`, the event-source choice in
[#130](https://github.com/Kylevdm/ha-spark/issues/130) changes materially.
Cancellation observation is not sharply testable on demand — it requires a real
changed/cancelled event and is best folded into the supervised-event work rather
than a standalone ticket.
