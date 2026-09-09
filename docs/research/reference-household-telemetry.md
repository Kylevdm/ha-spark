# Reference-household telemetry probe

Date: 2026-09-09  
Subject: [Telemetry probe: what signal do the two reference households expose?](https://github.com/Kylevdm/ha-spark/issues/76)

## Answer

Both reference households now expose enough kinds of signal to identify usable
battery capacity and lumped charge efficiency: reported SoC plus signed battery
power, with deep cycles present. This corrects the ticket's premise that the
AlphaESS household has no live rate. Its current local integration exposes a
high-resolution signed battery-power sensor as well as SoC, grid power, PV power,
and SoH.

The immediate constraint is history depth, not signal shape. Kyle's recorder has
about 17 calendar days of the relevant series and the AlphaESS household about
11. That is short of the “weeks of data” requirement established by the ML
survey, especially for a fit intended to survive seasonal and operating-mode
variation. ha-spark should therefore accumulate its own bounded training history
or require a longer recorder window before enabling battery identification. The
fitter must fail closed to configured constants until its data-volume, cycle-depth,
and sanity gates pass.

PV forecast correction is feasible at both sites, but the forecast providers are
not the same. Kyle has paired Solcast power forecasts and actual Growatt PV power.
The AlphaESS household has paired forecast.solar output and actual AlphaESS PV
meter power, not Solcast. The learning seam should accept a generic
forecast-versus-actual pair rather than require Solcast specifically.

## Aggregate observations

| Signal | Kyle — Solis, 26.88 kWh | Reference AlphaESS household, ~10 kWh |
|---|---|---|
| SoC | Present; 98 recorded changes in 24 h; median change interval 370 s | Present; 353 recorded changes in 24 h; median change interval 119 s |
| Battery power | Present and signed; range −3.23 to +3.22 kW over 7 days; median recorded change interval 15 s | Present and signed; range −4.79 to +4.97 kW over 7 days; median recorded change interval 2.1 s |
| Grid power | Present; median recorded change interval 15 s | Present; median recorded change interval 2.0 s |
| Actual solar | Growatt PV power present; median recorded change interval 1 s | AlphaESS PV-meter power present; median recorded change interval 2.0 s |
| Forecast solar | Solcast power-now present; changes every 5 minutes | forecast.solar power-now present; changes hourly; no Solcast entity found |
| Retained history | About 17 calendar days across SoC, power, actual PV, and forecast PV | About 11 calendar days across SoC, power, actual PV, and forecast PV |
| Cycle depth in retained SoC | 6 days span at least 40 percentage points; 3 span at least 60; observed range 22–100% | 10 days span at least 40 percentage points and at least 60; observed range 9.6–100% |
| SoH / nominal capacity | SoH exposed (97% at probe time); no inference about nameplate capacity needed for this ticket | SoH exposed directly (100% at probe time); no nominal battery-capacity entity found |

“Recorded change interval” is the interval between recorder rows, not a claim
about the integration's configured polling interval. A sensor that polls without
changing state does not necessarily create another recorder row.

## Consequences for the three constants

### `battery_capacity_kwh`

Feasible at both sites once enough history has accumulated. Both have signed
power, SoC, and unusually useful deep cycles. Direct SoH does not make capacity
learning moot: SoH is a percentage and the AlphaESS integration does not expose
nominal or usable battery capacity. SoH can be an input or cross-check, not the
answer by itself.

### `charge_efficiency`

Feasible at both sites in principle. Signed battery power distinguishes charge
and discharge, while grid/PV power and cumulative battery charge/discharge energy
offer possible consistency checks. The current recorder windows are too short for
a trustworthy production fit. Polarity must be learned or configured per driver;
the two integrations need not use the same sign convention.

### `solar_haircut_k`

Feasible at both sites from contemporaneous forecast and actual-PV series. Kyle's
pair is Solcast/Growatt at 5-minute/approximately 1-second recorded-change cadence.
The AlphaESS pair is forecast.solar/AlphaESS at hourly/approximately 2-second
cadence. Training should resample both sides to a common interval and retain the
forecast as issued, rather than comparing actual generation with a forecast value
that was revised after the fact.

## Recommendation to the ML-learning decision

Treat battery identification as a generalisable post-v1 technique, not a
Kyle-only experiment, but gate it on locally measured evidence:

- persist a privacy-local, bounded training series independently of HA recorder
  retention, or document a minimum recorder retention of several weeks;
- require signed power, a minimum number of usable observations, multiple deep
  cycles, and plausible fitted bounds before publishing learned constants;
- keep configured constants unchanged when any gate fails;
- define PV correction against a provider-neutral forecast/actual interface;
- use reported SoH only as a diagnostic or prior unless nominal capacity is also
  available.

The AlphaESS household is therefore a useful graceful-degradation case for
history sufficiency, not for telemetry absence.

## Method and privacy

The probe used authenticated, read-only Home Assistant REST calls to inventory
current entity metadata and query recorder history. It inspected selected energy
series over bounded windows and emitted aggregates only: availability, units,
counts, intervals, extrema, sign counts, retained-day counts, and daily SoC spans.
No raw history, endpoint, token, serial number, meter identifier, or household
schedule is included in this document or committed to the repository.
