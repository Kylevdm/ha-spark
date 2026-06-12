# Changelog

## 0.2.0

- Live supply guard (Phase 3, EV-aware): when `grid_power_entity` is set, the
  daemon throttles the battery's timed-charge current whenever whole-house AC
  draw exceeds `supply_max_current_a` (default 75 A) — e.g. an EV dispatch
  landing mid-window — and restores it as headroom returns. Gated by
  `proactive_mode` like all writes; disabled until the sensor is configured.
- Plan report now shows the EV energy Octopus plans to deliver across the
  upcoming dispatches.
- `ha-spark health` gains a supply-guard sensor check.

## 0.1.0

- Initial add-on release: daily charge-plan daemon (`ha-spark run`) with
  startup health report, full options schema (planner knobs, tariff, entity
  IDs, Octopus API, Ollama), persistent SQLite store under `/data`.
