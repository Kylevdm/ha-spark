# v1 release requirement carry-over (#258)

#247 supersedes #94. The current checklist is [releasing.md](releasing.md).
Unchecked gates require evidence; this change implements no runtime behavior.

| Legacy requirement | Disposition and reason |
| --- | --- |
| Competitive MVP definition / #43 | Replaced as the programme definition by #247; compatible feature outcomes below remain. |
| Phase 10 version assignments and rc reinstall | Dropped as a future sequence: historical releases are not the new ingress/integration sequence. Retired rc tags remain unused; #247 owns sequencing. |
| ruff, strict mypy, pytest | Retained as release quality gates. |
| Simulate default, action logging, zero writes | Retained for automatic control; current log marker is `[SIMULATE]`. |
| Observe/supplier authority prevents writes | Retained for automatic control. |
| on + ha_spark, read-back, failure isolation | Retained for automatic control across UI/CLI and integration actuation. Standalone/manual contracts await #248/#255; no invented bypass. |
| Invalid SoC prevents actuation | Retained for normal programming, reconciled with the existing narrowly guarded/verified Solis integrity fallback (#112/#119); no AlphaESS fallback. |
| No secrets in logs/plans/agent surface | Retained; new UI/integration surfaces must preserve it. |
| Flat config boots unchanged; options.json never rewritten | Replaced with UI-owned config and bootstrap-only Supervisor validation under #253. #247 explicitly requires no general migration for one household. |
| Bad tariff provider rejected at startup | Retained; supported configuration surfaces must agree with #253. |
| fixed/dynamic/octopus_intelligent prices; fixed fallback | Retained: tariff providers remain consumed sources, including Octopus integration. |
| AlphaESS setbatterycharge services.yaml or experimental label | Replaced with native Modbus hardware/control validation (#250), read-back and failure handling. The external cloud service is superseded. |
| Agent authority, gated writes, LLM cannot call services, token auth | Retained and extended to ingress UI and authenticated discovery/API (#252). #248/#255 own unresolved manual contracts. |
| Derived base load and improved forecast error | Retained: independent of transport/UI. |
| Half-hourly replan, day-of events/plug-in, unchanged setpoints zero writes | Retained: deterministic planner behavior remains required. |
| Sentence-shaped reservation reasons | Retained for explainable plans and UI. |
| Real Solis Axle event, limits, funded house, malformed payload fallback | Retained; #128 remains a scoped supervised prototype, not proof of unattended readiness. #241 supplies shared effective limits. |
| V2L only for cheaper reservation refill, plugged-in gating, notice | Retained; native integration does not replace planner economics or availability rules. |
| Readiness measures simulate history; never flips mode | Retained; readiness cannot grant authority. |
| Phone digest in old release definition | Retained as a compatible feature requirement; ingress statistics/chat do not explicitly supersede delivery. #247 must sequence it. |
| config version on master, changelog/docs/schema | Retained, with bootstrap-only schema and UI config contract per #253. |
| Matching pushed tag | Retained and extended to HACS GitHub Release, lockstep versions and API handshake (#252). |
| Clean Supervisor build from pinned tag | Retained; companion integration install/compatibility also requires #252 validation. |

## Outstanding work and supersession

- #128 and #165 carry into #247 without changing scope, status or dependencies.
  #165 must avoid guard/apply charge-current races; its design is not settled here.
- #59 is already closed as superseded by #247/#257. Preserve driver/provider
  suggestions, entity mapping, capability coverage, presets and multi-device
  onboarding. Existing discovery/presets are not evidence that the rest shipped.
- #241 is a required input to the complete map spec: bank/inverter/site limits,
  shared-bank allocation, major-load headroom and stale-reading policy.
- Config design (#253), integration ownership (#248), manual override (#255),
  full Solis parity and phase/version sequencing (#247) remain with their owners.
  No implementation slices, hardware validation or release are created here.
