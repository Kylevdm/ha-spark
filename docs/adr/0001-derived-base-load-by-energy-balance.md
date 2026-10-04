# ADR-0001: Derived base load by energy balance

Status: Accepted (2026-07-12)

## Context

ha-spark's load forecast has always used a single user-supplied consumption
sensor. On the maintainer's site, and according to the Competitive MVP epic
(#43) on most real installs, that sensor includes battery charging. The
forecast therefore follows the charge ha-spark itself scheduled the night
before, and it can't tell "the house is using a lot" from "the battery is
charging". The same error is in the historical load statistics, so forecasting
and model evaluation are both judged against bad data. **Base load**, what the
house consumes with all plannable sources and sinks removed (see CONTEXT.md),
is a separate and cleaner quantity, and it is the right input to the forecast
chain.

## Decision

Derive base load by energy balance instead of trusting the consumption
sensor:

```
base load = grid import − grid export + solar + battery discharge
            − battery charge − EV charge
```

This is a pure function over Home Assistant's long-term component energy
statistics (grid import/export, solar production, battery charge/discharge,
EV charge), not a live calculation. It:

- writes the existing ha-spark house-load statistic, so the rest of the
  forecast chain (ML, then slot profile, then daily median, then baseline) is
  unchanged and only its input data improves;
- backfills history, so past load statistics are recomputed the same way and
  forecasting and model evaluation stop being judged on bad history;
- takes each component's sign convention from **explicit configuration**,
  shown during onboarding and never inferred or guessed, because a mis-signed
  export or battery-charge sensor would otherwise break the balance without
  any error.

## Alternatives considered

- **Trust the consumption sensor as-is.** The status quo. Rejected because it
  is the exact problem this ADR exists to fix. The sensor can't tell base load
  from battery charging, so the forecast follows its own overnight setpoints.
- **Subtract battery only** (`base load = consumption − battery charge`).
  Simpler, and it removes the most visible error. Rejected as insufficient
  because it leaves EV charging in the data, and it doesn't generalise to a
  zero-export site, where the export term also matters for the balance. Once
  ha-spark reads the component statistics anyway, a full energy balance costs
  little more.

## Consequences

- The relevant component sensors must have Home Assistant long-term
  statistics. Onboarding checks this rather than assuming it.
- Each component needs sign-convention configuration (grid import/export
  polarity, battery charge/discharge polarity) rather than one auto-detected
  value.
- The forecast chain's model code is unaffected, since only its input series
  changes, so the ML/profile/median fallback chain and its tests carry over
  unchanged.
- Backfill rewrites the historical load statistics. That is a one-time
  recomputation you can audit, not a silent change in behaviour.
