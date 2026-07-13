# ADR-0001: Derived base load by energy balance

Status: Accepted (2026-07-12)

## Context

ha-spark's load forecast has always trusted a single user-supplied
consumption sensor. On the maintainer's site (and, per the Competitive MVP
epic, on most real installs) that sensor is polluted: it includes battery
charging, so the forecast chases setpoints ha-spark itself created the
previous night, and it cannot distinguish "the house is heavy" from "the
battery is charging." The same pollution corrupts historical load
statistics, so both forecasting and model evaluation are judged against
corrupted data. **Base load** — what the house consumes with all plannable
sources and sinks stripped out (see CONTEXT.md) — is a distinct, cleaner
quantity, and it is the correct input to the forecast chain.

## Decision

Derive base load by energy balance instead of trusting the consumption
sensor:

```
base load = grid import − grid export + solar + battery discharge
            − battery charge − EV charge
```

This is a pure function over Home Assistant's long-term component energy
statistics (grid import/export, solar production, battery charge/discharge,
EV charge), not a live/instantaneous calculation. It:

- writes the existing ha-spark house-load statistic so the rest of the
  forecast chain (ML → slot-profile → daily median → baseline) is unchanged
  — only its input data becomes clean;
- backfills history, so past load statistics are recomputed the same way and
  forecasting/model evaluation stop being judged on corrupted history;
- treats the sign convention of each component as **explicit configuration**
  surfaced by onboarding, not inferred or guessed — a mis-signed export or
  battery-charge sensor silently breaks the balance otherwise.

## Alternatives considered

- **Trust the consumption sensor as-is.** Status quo. Rejected: this is the
  exact pollution problem this ADR exists to fix — the sensor cannot
  distinguish base load from battery charging, so the forecast chases its
  own overnight setpoints.
- **Subtract battery only** (`base load = consumption − battery charge`).
  Simpler, and removes the most visible pollution source. Rejected as
  insufficient: it leaves EV-charging pollution in place, and doesn't
  generalise to a zero-export site where the export term also matters for
  balance — a full energy-balance derivation costs little more once the
  component statistics are being read anyway.

## Consequences

- Requires the relevant component sensors to have Home Assistant long-term
  statistics available; this is a prerequisite verified during onboarding,
  not assumed.
- Requires per-component sign-convention configuration (grid import/export
  polarity, battery charge/discharge polarity) rather than a single
  auto-detected value.
- The forecast chain's model code is unaffected — only its input series
  changes — so the existing ML/profile/median fallback chain and its tests
  carry over unchanged.
- Historical load statistics are rewritten via backfill, which is a
  one-time, auditable recomputation rather than a silent behaviour change.
