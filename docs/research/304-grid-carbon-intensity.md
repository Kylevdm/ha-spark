# Regional grid carbon intensity for the plan view

Date: 2026-10-14
Ticket: [#304](https://github.com/Kylevdm/ha-spark/issues/304) (map [#247](https://github.com/Kylevdm/ha-spark/issues/247); plan visual [#256](https://github.com/Kylevdm/ha-spark/issues/256))

Question: what source should supply the plan view's **display-only** half-hourly
carbon strip? It covers today's history and the plan horizon (to about tomorrow
23:30 local). For the NESO Carbon Intensity API, or a better primary source, this
note establishes the endpoints, how a site maps to a region, resolution and horizon,
index bands, limits and terms, forecast drift, and offline behaviour. The planner
does not consume carbon.

Sources:

- NESO Carbon Intensity API reference (OpenAPI render):
  <https://carbon-intensity.github.io/api-definitions/>, linked from
  <https://api.carbonintensity.org.uk/> ("API docs")
- carbonintensity.org.uk landing page: <https://carbonintensity.org.uk/> ("Site")
- API Terms of Use: <https://github.com/carbon-intensity/terms/>, linked as
  `terms.carbonintensity.org.uk` ("Terms")
- NESO, *Carbon Intensity Forecast Methodology*, May 2024
  ([national PDF](https://api.neso.energy/dataset/f406810a-1a36-48d2-b542-1dfb1348096e/resource/36638178-e993-4d34-afdc-4ada83993585/download/neso-ci-national-methodology_v2.pdf), "National methodology")
- NESO, *Carbon Intensity Regional Forecast Methodology*, May 2024
  ([regional PDF](https://api.neso.energy/dataset/bf3d723b-b4ae-4ec7-9fc9-e584c651a9da/resource/1d817f4c-a126-45c8-baf0-218bf57d37be/download/neso-ci-regional-methodology_v2.pdf), "Regional methodology")
- NESO data portal, Regional Carbon Intensity Forecast dataset:
  <https://www.neso.energy/data-portal/regional-carbon-intensity-forecast>
- **Live probes** of `api.carbonintensity.org.uk` on 2026-10-14 around 19:10Z ("Probe"). Example
  postcodes are the API docs' own (`RG10`) or a public landmark (`SW1A`). Region 12
  was used only because the docs' `RG10` example maps to it.
- HA Electricity Maps integration: <https://www.home-assistant.io/integrations/co2signal/>
  and `home-assistant/core` `homeassistant/components/co2signal/` (master, 2026-10-14)
- Secondary, labelled where used: [nmpowell/carbon-intensity-forecast-tracking](https://github.com/nmpowell/carbon-intensity-forecast-tracking)
  (a third-party archive of NESO forecasts)

## 1. Answer in brief

Use the **NESO Carbon Intensity API, regional, addressed by `regionid`**. Make one
`{from}/{to}` request every 30 minutes, which returns today's history and about
51 h of forecast in one payload. Persist the result in SQLite and render the API's
own `index`. It is keyless, free, CC BY 4.0, and needs no new dependency.
**Regional data has no `actual` field.** The history half of the strip is NESO's
latest regional *estimate*, not a metered actual, and the UI must not call it
"actual". The HA Electricity Maps integration is not a substitute: it gives a single GB
zone with no forecast. Details and caveats follow.

## 2. Endpoints and response shapes

Base URL `https://api.carbonintensity.org.uk`. All times are UTC, in the format
`YYYY-MM-DDThh:mmZ` (API docs: "All times provided in UTC (+00:00)").

Regional endpoints (API docs, "Carbon Intensity - Regional"):

| Path | Returns |
|---|---|
| `GET /regional/regionid/{regionid}` | current half hour, one region |
| `GET /regional/postcode/{postcode}` | current half hour, region for an outward postcode |
| `GET /regional/intensity/{from}/fw24h/regionid/{id}` | next 24 h |
| `GET /regional/intensity/{from}/fw48h/regionid/{id}` | next 48 h |
| `GET /regional/intensity/{from}/pt24h/regionid/{id}` | past 24 h |
| `GET /regional/intensity/{from}/{to}/regionid/{id}` | arbitrary range |
| each of the above with `/postcode/{postcode}` instead of `/regionid/{id}` | same, located by postcode |
| `GET /regional`, `/regional/england`, `/scotland`, `/wales` | all regions, or one country, current half hour |

National (GB) equivalents are `/intensity`, `/intensity/date`, `/intensity/{from}/fw48h`,
`/intensity/{from}/{to}` and so on. The docs cap the national `{from}/{to}` range at
**14 days**. No cap is documented for the regional range, and a 24-day regional request
succeeded (Probe).

**Example (Probe):** `GET /regional/intensity/2026-10-14T00:00Z/fw48h/regionid/12`

```json
{"data": {"regionid": 12, "dnoregion": "SSE South", "shortname": "South England",
  "data": [
    {"from": "2026-10-13T23:30Z", "to": "2026-10-14T00:00Z",
     "intensity": {"forecast": 132, "index": "moderate"},
     "generationmix": [{"fuel": "biomass", "perc": 16.1}, {"fuel": "gas", "perc": 27.8}, "..."]},
    "..."]}}
```

Shape notes, all from the Probe:

- **The current-half-hour endpoints wrap `data` in a list**:
  `{"data":[{"regionid":..,"data":[...]}]}`. The range endpoints return an object:
  `{"data":{"regionid":..,"data":[...]}}`. The postcode variants add `"postcode"`.
- `intensity` has **only `forecast` and `index`** in every regional row. Across
  12,000+ regional rows from 2026-10-01 to 10-14, the key set was always `{forecast, index}`. The
  schema's `actual` ("The estimated actual Carbon Intensity for the half hour", API docs
  *Schemas*) appears only on national rows.
- `{from}` snaps back one period. `fw48h` from `19:00Z` starts with the `18:30Z–19:00Z`
  row, and a `fw48h` call returned 97 rows rather than 96. Key rows by `from` and de-duplicate.
- **`fw48h` truncates at `from + 48 h`, but `{from}/{to}` returns everything published.**
  At 19:10Z on 10-14, `{now}/{now+6d}` returned rows up to `2026-10-16T22:00Z`, which is
  about 51 h ahead. `fw48h` stopped at `10-16T19:00Z`. `fw24h` from a date past
  the published end returned `{}`.
- Errors are JSON `{"error":{"code":"400 Bad Request","message":"..."}}`. The Probe saw a
  transient `500 "Requested resource not found."` for `regionid/17` that succeeded on retry.
- **A 200 can carry a literal `null` body.** `GET .../fw48h/postcode/SW1` returned
  `200` with body `null`, while `GET /regional/postcode/SW1` returned `400 "No postcode
  match can be found."`. The API docs give `SW1` as a valid example.
- No `Cache-Control` or rate-limit headers were returned (served via CloudFront and API
  Gateway, with `access-control-allow-origin: *`).

## 3. Mapping a site to a region, and what it sends

There are 14 DNO regions (ids 1–14). Ids 15/16/17 are England, Scotland and Wales,
and id 18 is GB. The ids and their shortnames come from the API docs *Regions* table
and the Regional methodology, Figure 1. "Regions are divided according to
Distribution Network Operator (DNO) boundaries" (Regional methodology; Site). The live
`/regional` lists id 6 as "North Wales & Merseyside", where the docs table says
"North Wales". Id 18 (GB) is live but not in the docs table. The `regionid`
validator message says "1-17" even though 18 works (Probe).

| Option | What leaves the house | Notes |
|---|---|---|
| `/regionid/{n}` | an integer 1–14, the same for millions of homes | No location beyond DNO region. **Preferred.** |
| `/postcode/{outward}` | the outward code (district, e.g. `RG10`). The docs say "Do not include full postcode, outward postcode only." | A full postcode returns 400 (Probe). This is finer-grained than the region and is sent on every call if used for polling. |
| Electricity Maps (§8) | lat/lon or the HA home location, plus an API key | Not needed here. |

No NESO endpoint maps lat/lon to a region, so HA's `latitude`/`longitude` cannot be
used directly. The minimal-disclosure path is to have the user **pick the region from a
list of 14 names** in the add-on options. A postcode is then never sent. For users who
don't know their DNO region, DOCS.md can point them to the
"Find My Region" postcode search on carbonintensity.org.uk. That lookup happens in their
browser, not in the add-on. **Unverified:** how NESO assigns an outward code that
straddles a DNO boundary. The docs do not say.

## 4. Resolution and horizon

- **30-minute** settlement periods throughout (Regional methodology, Step 1: "forecast
  two days ahead at 30-min temporal resolution").
- Published horizons disagree. The API docs summary and both methodology PDFs say
  **48 hours** ahead. The Site says "96+ hours ahead". The **API served about 51 h**
  (Probe, above). Treat 48 h as the guaranteed horizon.
- Forecasts "are updated every 30 mins using a nowcasting technique" (Regional
  methodology, Step 1; Site). The NESO data portal lists the regional dataset as
  updated "Hourly", which is a different distribution channel.
- **Coverage of the plan horizon:** the worst case is just after local midnight,
  when "tomorrow 23:30 local" is about 47.5 h away. That fits inside 48 h with little
  margin. If a fetch fails for a few hours, the tail of the strip will be missing
  (see §7).
- History: the regional range endpoint served 2025 data (Probe). The Site says
  downloadable data starts 2017-09-26. Today's history is always available.

## 5. Index bands

National methodology, Table 2 (gCO2/kWh). **The bands tighten every year** to 2030:

| Year | very low | low | moderate | high | very high |
|---|---|---|---|---|---|
| 2025 | 0–29 | 30–99 | 100–179 | 180–250 | 251+ |
| **2026** | **0–24** | **25–89** | **90–169** | **170–230** | **231+** |
| 2027 | 0–19 | 20–79 | 80–159 | 160–210 | 211+ |
| 2030 | 0–4 | 5–49 | 50–129 | 130–150 | 151+ |

(The table runs 2017–2030. Nothing is published for 2031 onward.)

Probe check of the API's `index` against the table:

- **Regional (2026-10-01..14, 11,898 rows):** 11,879 matched the 2026 bands. All 19
  mismatches were `forecast: 230` labelled `very high`, where the table says `high`.
  The same edge appeared in June 2025: three `250 → very high` rows. The API
  apparently applies the band to an unrounded value, or uses an exclusive upper edge.
- **National:** where `actual` exists, `index` follows the **actual** (659/661 rows),
  not the forecast (575/661).

Consequence: **render the API's `index` string; don't recompute bands.** Recomputing
needs a per-year table in code, gets the edge wrong, and stops at 2030.
Validate `index` against the five-value enum, and treat any other value as "no band"
for that period.

## 6. Auth, rate limits, licence and terms

- **Auth:** none. No key, no account (API docs; Probe).
- **Rate limit:** **unpublished.** The Terms say only "National Energy System Operator
  applies a rate limit and may block applications that make a large number of calls to
  the API", and forbid use "that undermines the stability of the host servers". No
  headers expose the limit (Probe). One call per 30 minutes per household is about 48
  per day, well inside any plausible limit, but that is an inference.
- **Licence:** "CC BY 4.0" (API docs header; Site; Terms). Attribution is required,
  so the UI should credit "Carbon intensity: NESO Carbon Intensity API (CC BY 4.0)". The NESO
  data-portal copy of the regional dataset is under the separate "NESO Open Data Licence",
  which does not apply to the API.
- **Terms that bind:** do not imply NESO endorsement, and seek written approval before
  using NESO's logo or word mark (Terms §1, §2). Data is provided "as is" and "might have
  bugs, inaccuracies or interruptions" (Terms §6). NESO "may change, suspend, or
  discontinue any aspect" without notice (Terms §4). The API is free (Terms §5).
- **Secrets:** none are involved, so the no-secret-logging rule is trivially met.

## 7. Accuracy, and whether regional actuals exist

**Regional actuals do not exist in the API.** Every regional row carries only
`forecast` (§2). The methodology says the regional model forecasts demand and
generation per region, then derives consumed intensity from a reduced-network AC power
flow (Regional methodology, Steps 1–6). NESO publishes no regional metered
equivalent.

What the past regional values are: a Probe comparison of region 18 (GB) past rows
with the national series (2026-10-01..14, 661 periods) found a mean absolute
difference of **3.9** gCO2/kWh to the national *actual* and **9.1** to the national
*forecast*. The past regional values are therefore revised towards metered data after
the fact, but are not identical to it. The third-party tracker says each period is
"revised for 24 hours afterward". Label the history segment **"estimated"**.

**Published forecast-vs-actual accuracy: none from NESO.** The methodologies give no
error figures. They say only that the forecast "is particularly sensitive to
short-term forecast errors in demand, wind and solar generation" and is "likely to be
less accurate than forecasts such as electricity demand" (National methodology). The
API **overwrites** forecasts: it keeps only the latest value per period. Day-ahead
drift therefore cannot be measured from it after the fact.

Numbers available, all **national** only:

- Probe, final forecast vs actual, 2026-10-01..14 (661 periods): **MAE 10.6**,
  bias −3.0, p95 32, max 103 gCO2/kWh. The forecast and actual fell in **different
  bands in 84 of 661 periods (13 %)**. This is the *nowcast-converged* forecast,
  which understates the error the strip shows a day ahead.
- Third-party tracker, which archives forecasts as published at all horizons:
  "mean 24.3782" gCO2/kWh absolute error over n = 1,378,740 forecast points. The
  2026-10-06..13 daily MAE ranged from 8 to 30. This is secondary and unverified here, but it is
  the only multi-horizon figure found.

**No regional drift figure exists.** Regions are more volatile than GB as a
whole. Across the 14 DNO regions, the spread in a single half-hour had a **median of
332 gCO2/kWh** (Probe). This range spans every band, and it is why regional beats
national for this strip. Regional day-ahead error is plausibly larger than the national
24 g, but that is unquantified.

Practical reading: at a day's range, the band is the right granularity to show. A
forecast-period colour is "probably this band, possibly a neighbour". Show the
gCO2/kWh figure only on hover or tap, not as a headline.

## 8. Caching, staleness and offline behaviour

The add-on must keep working offline. Carbon is display-only, so the rule is
to **degrade to less strip, never to wrong strip, and never block the plan view**.

- **One fetch per 30 min**, a few minutes after each settlement boundary:
  `GET /regional/intensity/{local-midnight-today as UTC}/{now + 52h}/regionid/{id}`.
  This single call covers both history and forecast, and it is about 140 rows (~53 KB with
  `generationmix`). Do not fetch on page load. The ingress view reads the store.
- **Persist** in the existing SQLite store, one row per `(regionid, period_from_utc)`:
  `forecast_g` (int), `index` (enum or null), and `fetched_at`. Upsert on every
  successful fetch, so the latest NESO value wins and past periods converge to the
  revised estimate. Prune rows older than about 2 days. Persisting across restarts means a
  restart never shows an empty strip and needs no catch-up burst.
- **Validate** with pydantic: accept the object form; treat `null`, `{}`, a
  non-JSON body, a missing key, a non-numeric `forecast`, an unknown `index`, or an HTTP error
  as "fetch failed". **Keep the cached rows** and do not clear them. Clamp
  implausible values (negative, or above about 1000) to "no band". Never let a carbon failure
  surface into planning, logs at error level, or health exit codes beyond a warning.
- **Retry:** use the normal 30-minute cadence. Do one quick retry on 5xx, given the Probe's transient
  500. No exponential storm is needed at this call rate.
- **Presentation:**
  - Past periods are drawn as solid bands, labelled "estimated", per §7.
  - Future periods are drawn as lighter or hatched bands, labelled "forecast".
  - Periods with no cached value are drawn as an empty, neutral strip. **Never extrapolate**
    or carry a value forward.
  - Stale data: if the last successful fetch is more than about 90 minutes old, show
    "carbon data as of HH:MM". Future rows remain the last-known forecast and are still
    useful, because the forecast was published up to 48 h ahead.
  - If no row covers "now", hide the strip with a one-line "carbon data unavailable".
- **Off by default.** Leave the strip disabled until a region is chosen, so a fresh install
  makes no call to a new endpoint. That matches "outbound only to configured endpoints"
  and the README's list of what ha-spark contacts.

## 9. Existing HA carbon sources

| | NESO API direct | HA core **Electricity Maps** (`co2signal`) | Community `carbon_intensity_uk` custom components |
|---|---|---|---|
| Source | NESO (system operator) | Electricity Maps (commercial aggregator) | NESO API |
| Key | none | **free API key required** | none |
| GB granularity | 14 DNO regions | `GB` zone (no GB sub-zones mentioned in HA docs) | regional by postcode |
| Forecast | ~48 h, half-hourly | **none**. HA calls only `fetch_latest_carbon_intensity` every 15 min (`coordinator.py`) | yes (attributes) |
| History | API, any range | only the HA recorder's samples of the current value | recorder |
| Limits | unpublished, keyless | free tier "50 requests per hour", one zone (HA docs) | n/a |
| Status | first-party, live | HA core, maintained | `jfparis/sensor.carbon_intensity_uk` last pushed 2024-12-05, 9 stars. The `alanmcgore` fork was last pushed 2022-10-10. Neither is in HA core. |

Electricity Maps fails the brief on two counts: it has no forecast, so the plan-horizon half of
the strip would be empty, and it offers national-only GB granularity. It would also add a
user-supplied secret. Electricity Maps' own pricing page lists forecasts among its paid
signals. I could not open its free-tier docs (the docs URL returned 403), so "no forecast
on free tier" rests on the HA integration's code, which never requests one.
The community components wrap the same NESO API, but they are unmaintained and not installed
by default, so depending on them adds a fragile hop for no gain. ha-spark already speaks
httpx and pydantic, so a direct client is about one small module and **no new
dependency**.

## 10. Recommendation

1. **Source:** NESO Carbon Intensity API, regional range endpoint by **`regionid`**.
   Use one request per 30 min to `{local midnight}/{now+52h}`.
2. **Location:** a new add-on option, `carbon_region`, an enum of the 14 DNO region
   names (stored as id 1–14), **unset by default, with the strip off**. No postcode
   and no lat/lon leave the house. DOCS.md links the NESO postcode finder for users
   who need help choosing. Add `carbon_intensity_api_url` (default
   `https://api.carbonintensity.org.uk`), following the `octopus_api_url` /
   `axle_api_url` pattern, so the endpoint is configured rather than hard-wired.
3. **Data:** render the API's `index`. Don't recompute the year bands. Store
   `forecast_g` for the tooltip. Label the past segment "estimated", not "actual".
4. **Resilience:** a persisted SQLite cache, pydantic validation, keep-last-good,
   never extrapolate, an "as of" stamp when stale, hide when nothing covers now. Carbon errors
   never touch the planner or actuation path. The planner doesn't read the table at all.
5. **Attribution:** show a "NESO Carbon Intensity API, CC BY 4.0" credit in the plan view
   footer. Use no NESO logo.
6. **Docs:** when it ships, add NESO to the README's "ha-spark contacts only..."
   sentence.

## 11. Open questions

- **History label.** Should the "today so far" segment show NESO's *regional
  estimate* (consistent with the forecast, recommended) or the *national actual*
  (metered, but a different geography)? The two cannot be mixed honestly on one strip.
- **Default and onboarding.** Is the strip off until a region is picked (recommended),
  or should onboarding ask for the region up front?
- **Forecast tail margin.** The guaranteed 48 h only just covers "tomorrow 23:30"
  after midnight. Should the strip end at the last published period, or should the
  plan view's horizon be clipped to it? (Assumed: end at the last published period.)
- **Unknown limit.** NESO publishes no rate limit. If heavier use is ever planned,
  such as several regions or a faster cadence, ask NESO (contact in the API docs header).
- **Postcode straddling** DNO boundaries, and the `SW1` docs/API inconsistency, are
  unresolved but avoided by the `regionid` design.
