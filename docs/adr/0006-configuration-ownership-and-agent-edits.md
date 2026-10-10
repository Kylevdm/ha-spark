# Configuration ownership and agent edits

Status: accepted (2026-10-10), confirmed by the owner in [Config store: what leaves the Supervisor options, where it lives, and who edits it](https://github.com/Kylevdm/ha-spark/issues/253). This is a v1 planning contract, not implemented behavior.

The companion integration must execute protection and fallback while the add-on is unavailable. It therefore owns its device connections, validated device, bank, and site limits, fixed fallback charging schedule, and durable manual-control state. The add-on owns planning, tariff, UI, and agent settings. The ingress UI is the primary editor for both, but each field has one authoritative owner. This differs from a [Zigbee2MQTT-style add-on-owned device bridge](https://www.zigbee2mqtt.io/guide/usage/integrations/home_assistant.html) because the agreed fallback requirement survives an add-on outage.

## Stores and editors

| Data | Authority | Editors |
| --- | --- | --- |
| Device connection credentials and endpoints | Integration config entry data | HA config/reconfigure flow; ingress through a validated integration command |
| Device, bank, and site limits and owner-configured fallback schedule | Integration config entry options | HA options flow; ingress through the same validated integration command |
| Manual-control ownership and deadline | Integration durable runtime store | Integration authority commands only; not generic config |
| Planning, tariffs, UI, and agent settings | Versioned `/data/ha_spark_config.json` | Ingress, CLI, and approved agent changes through one validation/mutation service |
| Add-on credentials | Separate `/data/ha_spark_secrets.json`, mode `0600` | Owner-facing ingress secret controls only |
| Add-on startup diagnostics | Supervisor `log_level` option | Supervisor UI |

The integration never depends on a copied add-on configuration to run protection or fallback. When the add-on returns, it reads the integration's current revision; it does not overwrite HA-side edits made during the outage. The integration's connection data and options follow Home Assistant's [config-entry separation](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow/). Runtime deadlines are durable integration state rather than options.

The add-on settings file has a schema version and is validated as a whole with Pydantic before a write. Integration config edits use equivalent whole-document validation. Add-on writes use a lock and atomic replacement so an invalid or interrupted update leaves the last valid document active. Production settings do not take precedence from environment variables or the old Supervisor options; injected HA credentials and local-development overrides are separate bootstrap paths. The existing SQLite database remains for history, not settings. Secrets never appear in config GET responses, logs, errors, plan output, agent tool output, or config exports: readers receive set/unset status, and a masked value submitted back means unchanged. Secret rotation uses the same validated write path and protected file permissions. Integration secrets stay in its HA config entry and are never mirrored into the add-on.

`config.yaml` retains only the `log_level` option and its schema. Its fixed ingress and published-port metadata remains packaging configuration; the published agent port is still enabled in Supervisor Network settings. Every former `_OPTION_KEYS` field is classified by owner and moved to the appropriate store or retired. `_OPTION_KEYS` no longer defines the Supervisor schema. Replace the exact-parity test with checks that only bootstrap keys remain there, each runtime field has one owner, and secret and reload metadata are complete. The old `options.json` is not a write target or a runtime source after cutover. The one existing household cuts over through fresh-install onboarding; no importer or general migration framework is required.

## Activation and failure behavior

Ingress and HA flows call the same integration-side validation/update command for integration-owned fields. Ingress, CLI, and the agent path call the same add-on validation/mutation service for add-on-owned fields. A writer supplies the expected revision; concurrent edits cannot silently overwrite each other. Unknown or retired keys fail with a field error rather than being ignored. A failed validation, persistence, or integration command leaves the previous active configuration in force and reports the failure without echoing secrets.

Onboarding validates both components, persists the integration's connection and fallback first, then the add-on's planning settings, and shows a partial-setup repair state if the second step fails. It remains in Observe until the owner explicitly enters Protect or Optimise. A validated fallback schedule and required device read-back capability are prerequisites for those modes. A config save never constitutes an operating-mode or authority transition; [Operating modes and authority: transitions, persistence, and recovery](https://github.com/Kylevdm/ha-spark/issues/266) owns that lifecycle.

Safe planning and tariff edits take effect without an add-on restart: replace the live validated snapshot, invalidate affected plans, and replan under the existing authority gates. Connection, listener, or hardware changes declare a restart/reconnection requirement and do not claim to be active until verified. Integration fallback changes remain locally durable and validated before use. No edit weakens SoC, capacity, authority, read-back, or failure-isolation gates.

## Product-agent permission

The owner manages an agent-edit allowlist in ingress settings. It defaults to empty and can allow individual fields or groups, including device and fallback settings if the owner chooses. Credentials, the allowlist and agent access controls, and operating-mode/authority transitions are never agent-editable. The agent cannot change its own permissions. Existing read-only tools and validated context-fact entry retain their separate exposure policy.

For every proposed `set_config` change, the agent shows the exact effective before/after diff and its likely plan/control consequence in the agent conversation and asks for approval there. A trusted client confirmation must bind to the exact diff, settings revision, owner, and one use; a free-form agent claim that the user said yes is insufficient. The server rechecks the allowlist, validation, and revision at commit. If a client cannot provide trusted approval, it cannot commit a config change. `agent_exposure=read_write` alone does not confer approval. Direct hardware commands remain outside this config permission; the existing `run_plan` tool's actuation behavior needs a separate map decision.

This amends the v1 map's former exclusion of all LLM-proposed or LLM-applied configuration changes. An owner-approved, allowlisted configuration edit is in scope; direct LLM device writes and LLM-controlled operating-mode transitions remain out of scope.
