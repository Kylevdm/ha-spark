# Solcast API for a native integration: limits, endpoints, and polling

Date: 2026-10-08
Ticket: [#251](https://github.com/Kylevdm/ha-spark/issues/251) (map [#247](https://github.com/Kylevdm/ha-spark/issues/247))

Question: what does the Solcast API offer free home-user ("hobbyist") accounts,
how do the existing HA integrations poll within the limit, what does ha-spark read
today, and what must a native client provide to replace those reads?

Sources:

- Solcast developer docs, hobbyist section:
  <https://docs.solcast.com.au/docs/section/rooftop-sites-hobbyist> ("Hobbyist docs")
- Solcast home-user FAQ: <https://kb.solcast.com.au/home-hobbyists-faqs> ("FAQ")
- Solcast home PV terms of use, on <https://solcast.com/free-rooftop-solar-forecasting> ("Home terms")
- Solcast OpenAPI spec: <https://api.solcast.com.au/openapi/v1/openapi.json> and
  index <https://docs.solcast.com.au/llms.txt>
- BJReplay/ha-solcast-solar v4.6.2, commit
  [`d75208f`](https://github.com/BJReplay/ha-solcast-solar/tree/d75208f87c3ee9e3f298f16e56a303aec0282801)
  (2026-10-06). File refs below are under `custom_components/solcast_solar/`.
- ha-spark at `6436cc2` (master).

## 1. What a home account gets

| Item | Value | Source |
|---|---|---|
| Daily quota | **10 requests per UTC day**, shared across all sites | Hobbyist docs; FAQ ("a total of 10 API calls per UTC day") |
| Quota reset | UTC midnight | FAQ |
| Legacy quota | Early accounts got 50/day. The FAQ says Solcast "decrease[d] this amount to 10". BJReplay's README says early adopters keep 50. **Treat the limit as a user setting, defaulting to 10.** | FAQ; BJReplay `README.md` |
| Sites | **Up to 2 rooftop sites** (arrays) within 1 km of each other. Each site is one tilt/azimuth. | Hobbyist docs; Home terms |
| Resolution | 30-minute intervals (PT30M) only | Hobbyist docs |
| Forecast horizon | "from the present time up to 14 days ahead" | Hobbyist docs |
| Estimated actuals | "near real-time and past-7-days" | Hobbyist docs |
| Quota query | **None.** There is no endpoint that reports usage or the limit, so clients count calls themselves. | BJReplay `sites_cache.py` `_sites_usage` docstring |

With two sites, every refresh costs two calls, which leaves **5 refreshes per day**
(FAQ). The two-site cap is per account. BJReplay accepts several API keys, but its
README warns this "may breach Solcast terms" when the sites are within 1 km.

### Terms that bind a native client

The Home terms apply on top of the standard API terms:

- The data is for "non-commercial home use", and you may not share it "with any third parties"
  without written permission.
- Rooftop sites must be the user's own home residence.

What this means for ha-spark: each household brings **its own API key**, and the
add-on/integration calls Solcast directly from that household's HA. ha-spark
must never proxy, cache centrally, or redistribute forecasts. That is how
BJReplay works today, so the model is established. **Open uncertainty:** the
terms don't address open-source software that makes the calls on the user's
behalf. The established practice suggests it is fine, but this is not a legal reading.

## 2. Endpoints

Base URL: `https://api.solcast.com.au` (BJReplay `const.py`
`DEFAULT_SOLCAST_HTTPS_URL`).

Home accounts use the legacy `rooftop_sites` paths. These are **not in the
OpenAPI spec**: it lists only the commercial `/data/forecast/rooftop_pv_power`
(lat/lon) family, which home accounts can't use. The Hobbyist docs list two
endpoints. The site listing below comes from integration source.

| Call | Path | Documented by | Counts against quota? |
|---|---|---|---|
| List sites | `GET /rooftop_sites?format=json` | BJReplay only (`sites_cache.py` ~L1102). Solcast docs say the resource id comes from the Toolkit site page. | Unknown. BJReplay doesn't count it. It runs on every load, with a cache fallback. |
| Forecast | `GET /rooftop_sites/{resource_id}/forecasts?format=json[&hours=N]` | Hobbyist docs (format only). BJReplay passes `hours` (`fetcher.py` L678–679). | Yes. 1 call per site. |
| Estimated actuals | `GET /rooftop_sites/{resource_id}/estimated_actuals?format=json[&hours=N]` | Hobbyist docs | **Unclear.** BJReplay fetches it with `force=True`, outside its own counter. Its README says force-updating estimates "does not increment the API use counter". That describes BJReplay's local counter, not a Solcast statement. |

**Auth:** `Authorization: Bearer <key>` header, or an `api_key=` query
parameter (Hobbyist docs). BJReplay uses the query parameter and has to redact
the key in its debug logs (`fetcher.py` L695 `redact_msg_api_key`). **A native
client should use the header.** That keeps the key out of URLs, httpx logs and
error strings, as the CLAUDE.md secrets rule requires.

**Response shape** (from BJReplay's parser, `fetcher.py` L531–545; the Hobbyist
docs give no example response):

```json
{"forecasts": [{"period_end": "<ISO-8601 UTC>", "period": "PT30M",
                "pv_estimate": 0.0, "pv_estimate10": 0.0, "pv_estimate90": 0.0}]}
```

- `pv_estimate*` is **mean kW over the 30-minute period ending `period_end`**.
  BJReplay converts to kWh with `0.5 * kW` (`forecast.py` L290–291, L486) and
  derives `period_start = period_end - 30min`.
- `pv_estimate` is P50. `pv_estimate10` and `pv_estimate90` are P10 and P90. Estimated
  actuals carry only `pv_estimate` (BJReplay sets the 10/90 values to 0,
  `fetcher.py` L476–482).
- The forecast covers one site. A multi-site household sums the per-period values
  across sites (BJReplay `solcastapi.py` ~L897–907).

### Errors and retries (BJReplay `fetcher.py` L650–770)

- `429` with body `response_status.error_code == "TooManyRequests"` ("You have
  exceeded your free daily limit.") means **quota exhausted**. Stop until UTC
  midnight. BJReplay marks the key used-up.
- Any other `429` means **Solcast is busy**. Retry with back-off: up to 10
  tries, with a delay of `try × 15 s + rand(0–15) s`, all inside a 900 s
  timeout. BJReplay's comment: "Occasionally the Solcast API is busy, and
  returns a 429 status".
- `400/401/403/404/500` and connection errors are not retried. A `403` on the site
  list means a bad key.
- Only a `200` increments BJReplay's local usage counter. Whether Solcast charges
  failed calls against the quota is undocumented.

## 3. How the existing integrations poll

**oziee/ha-solcast-solar is gone.** The GitHub repo returns 404, and BJReplay's
README says it "is no longer being developed and has been removed". BJReplay (v4.6.2,
actively maintained, last push 2026-10-06) is the only HA integration to study.

BJReplay's scheduling (`updater.py` `_calculate_forecast_updates`, `enums.py`
`AutoUpdate`):

- Auto-update modes: `NONE` (the user automates it with the
  `solcast_solar.force_update_forecasts` / `update_forecasts` actions),
  `DAYLIGHT` (the default recommendation), and `ALL_DAY`.
- `divisions = api_limit // number_of_sites`. Polls are spaced evenly between
  sunrise and sunset (or across 24 h), using HA's astral sunrise/sunset.
  For 10 calls and 2 sites, that is 5 polls per day, about every 2–3 h in a UK summer.
- The usage counter resets at UTC midnight (`coordinator.py`
  `async_track_utc_time_change(hour=0, minute=0, second=0)`) and is persisted
  across restarts (`sites_cache.py` `serialise_usage`). This stops a restart
  storm from burning the quota.
- Every forecast poll requests `hours = ceil(end_of_day(+N) − now)` with N
  defaulting to 14 (`const.py` `DEFAULT_FORECAST_DAYS`, range 8–14). Estimated
  actuals are fetched once a day just after midnight, with `hours=168`
  (`fetcher.py` L210), if enabled.
- Forecasts are merged into a local JSON history cache rather than replaced
  (`forecast_entry_update`, `sort_and_prune`). Past periods keep their last
  forecast value, which feeds the Energy dashboard and BJReplay's dampening.

What it exposes (`sensor.py`, `forecast.py`):

- Day sensors such as `forecast_today` / `forecast_tomorrow` / `forecast_day_N`. The state is
  the day total in kWh **for the configured "key estimate"**
  (`use_forecast_confidence`, which can be P10, P50 or P90). It is not necessarily P50.
- Day attributes, each optional through `attr_brk_*` options: `estimate`,
  `estimate10` and `estimate90` (day totals in kWh), `detailedForecast` (a list of
  `{period_start (local tz), pv_estimate, pv_estimate10, pv_estimate90}` in **kW**),
  `detailedHourly`, per-site variants, and `analysis` (P10/P90 spread and confidence).
- Power and energy sensors (power now, next hour, remaining today, peak time and value),
  `api_used` / `api_limit`, last-updated with the next scheduled poll, and a
  dampening select. It also offers optional hard-limit (inverter clipping) and dampening
  modifications, both applied **before** the values reach the sensors.

## 4. What ha-spark reads today

All reads come from one entity, `solar_tomorrow_entity` (`config.py`). The preset
is `sensor.solcast_pv_forecast_forecast_tomorrow` (`presets.py`). Onboarding
finds it by keywords plus a `detailedForecast` attribute
(`onboarding_discover.py`).

| Read | Where | Used for |
|---|---|---|
| Entity **state** as a float (kWh) | `energy/sources.py` ~L507 `solar_kwh` | Tomorrow's day total, which becomes `PlannerInputs.solar_tomorrow_kwh`. The comment assumes it is the median. |
| Attribute `estimate10` / `estimate90` | `sources.py` ~L510–515 | Replaces the total when `solar_percentile` ≠ 50 |
| Attribute `detailedForecast[*].period_start`, plus `pv_estimate` / `pv_estimate10` / `pv_estimate90` | `sources.py` `_parse_detailed_forecast` | Per-slot **shape only**. `energy/solar.py` `distribute_solar` uses the values as relative weights, scaled to the day total, so kW-vs-kWh doesn't matter. Falls back to a half-sine 08:00–18:00. |

Then the planner applies `solar_haircut_k`: `effective_solar = solar × k`
(`planner.py`). Related settings in `config.py`: `solar_tomorrow_entity`,
`solar_percentile: Literal[10, 50, 90]` (default 50), `solar_haircut_k`
(default 1.0).

Two latent mismatches with BJReplay surfaced along the way. Both are flagged only; no change is made here:

1. If a user sets BJReplay's key estimate to P10 or P90, the entity state is no longer the median.
   When `solar_percentile=50`, ha-spark would then plan on P10 or P90 without
   knowing it.
2. BJReplay's dampening and hard limit alter the values before ha-spark sees
   them. With `solar_haircut_k` on top, the correction can stack twice.

## 5. What a native client must provide

To replace the reads in section 4 one for one, the native Solcast client (integration side,
per #247's split: I/O and entities, no decisions) must deliver the following:

1. **Tomorrow's 48 half-hour slot values in local time** for P10, P50 and P90:
   `period_end − 30 min`, converted to the HA timezone, kWh = `0.5 × kW`, and
   **summed across up to 2 sites**. Day totals at each percentile are derived from
   those slots. A single explicit percentile then replaces both the state-as-median
   assumption and the `estimate10/90` attribute lookup.
2. **Raw, undampened values.** ha-spark's `solar_haircut_k` (and any future
   calibration) owns correction. This avoids the double-correction in §4.
3. **Freshness metadata**: last successful fetch time, and calls used and limit for the
   UTC day. The add-on can then judge staleness and fall back. Today an unreadable entity
   becomes a 0 kWh total in `sources.py` (`_to_float(..., 0.0)`). That is the safe
   direction, because it charges more, but it is silent. A missing slot shape alone
   falls back to the half-sine.
4. **Persisted forecast cache.** Keep the last good forecast across restarts,
   because there is no cheap way to re-fetch. A restart must never spend quota by
   itself.
5. **Quota-aware polling**: a local counter persisted across restarts and reset at
   00:00 UTC, with `divisions = limit // sites`. The ha-spark-specific requirement is
   **one fresh poll before the evening plan**. Tomorrow's forecast drives
   overnight charge sizing, so the poll nearest the charge window matters most.
   (Whether evening polls improve the day-ahead forecast is untested.) Spread the remaining polls over daylight for intraday
   replans. The user sets the limit (default 10, 50 allowed).
6. **Correct 429 handling**: treat `TooManyRequests` as quota exhausted until UTC midnight,
   back off and retry on any other 429, and never retry 4xx auth errors.
7. **Site discovery** through `GET /rooftop_sites` in onboarding, so users enter only the
   API key.
8. **Security**: send the key as a `password` option or secret, in the `Authorization: Bearer`
   header only, and never log it. Validate every response field with
   pydantic and the tolerant coercion helpers. A malformed payload degrades to "stale", never to
   a crash. Outbound traffic goes only to `api.solcast.com.au`.

Optional, and not needed to replace the current reads: estimated actuals (7 days back), which
the "forecast accuracy" and "calibration drift" statistics in #247 would want.
Their quota cost is unconfirmed (§2), so budget for them as if each one costs a call until a
live test shows otherwise.

## 6. Open questions

- Whether `GET /rooftop_sites` and `estimated_actuals` calls count against the
  10/day quota. Solcast doesn't document either. This needs a live test against a
  counter, or a question to Solcast support.
- Whether failed (non-200) calls count.
- Whether `hours` is honoured on hobbyist forecasts. BJReplay sends it, but Solcast's
  hobbyist docs list only `format`. If it is ignored, a full 14-day payload comes back, which is harmless.
- How the Home terms ("no sharing with third parties") read for an open-source
  client acting on the user's own key. Established practice (BJReplay) says it is fine, but this is
  not confirmed with Solcast.
