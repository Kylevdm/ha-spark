Design decisions / clarifications from user

Proactivity behaviour

The agent should be architected to support fully proactive actions driven by the habit learner and/or LLM, but with a hard “dry‑run” / “observe‑only” mode for development and testing.

In this observe‑only mode, the system should compute and log what it would have done (habit‑driven actions, LLM‑driven service calls), but must not actually call any HA services.

There should be a single, explicit configuration flag controlling this (e.g. PROACTIVE_MODE = "off" | "simulate" | "on"), with "off" / "simulate" being the default in early phases.

The internal APIs (habit prediction, orchestrator tool layer) should be designed so you can flip between simulate and real actuation without changing core code paths: same decisions, just side‑effects suppressed and logged.

Deployment assumption (HA add‑on only)

Assume the system will run exclusively as a Home Assistant add‑on for now; standalone mode can be treated as a possible future extension, not something to support in the initial implementation.

Configuration should therefore assume:

Use of /data/options.json for user‑exposed options.

Use of SUPERVISOR_TOKEN for authenticating to Home Assistant Core via the supervisor proxy.

HA API base URLs should be hard‑defaulted for add‑on mode:

REST: http://supervisor/core/api

WebSocket: ws://supervisor/core/websocket (proxy path for /api/websocket from inside add‑ons).

You can still keep an internal abstraction for “HA base URL” and “WS URL”, but initial config resolution can treat the add‑on mode as the only supported runtime and fail fast if the expected env/paths (SUPERVISOR_TOKEN, /data/options.json) are missing.

Entity addressing

All tools and HA‑facing operations should use raw entity_id values.

There is no requirement (for now) to support fuzzy name resolution (“kitchen lamp” → light.kitchen_lamp); the LLM is expected to work directly with entity_ids in tool calls.

Tool schemas should therefore:

Accept entity_id as a string, not human‑readable labels.

Return results including entity_id and optionally friendly_name as data, but not depend on name‑based lookups.

Diagnostics / health

A dedicated CLI diagnostic entry point is desired (e.g. python -m ha_spark health or cli.py health).

This health/doctor command should:

Check that the HA REST API is reachable via the supervisor proxy and that authentication with SUPERVISOR_TOKEN works (simple /api/config or /api/ call).

Check that the HA WebSocket API is reachable via ws://supervisor/core/websocket and can complete the auth handshake.

Check connectivity to the configured Ollama endpoint (see LLM changes below) via /api/tags.

Verify that the SQLite DB path under data/ is writable and that migrations/initial schema can be applied.

Output should be human‑readable, suitable for quick debugging in an HA add‑on shell, and ideally return a non‑zero exit code when checks fail so it can be used by external health‑check tooling.

LLM topology change (no local small model)

Remove the “local small CPU‑only model” tier from the design; the only LLM tier should be a remote Ollama instance reachable over Tailscale (or equivalent private networking).

The router should therefore support just two tiers:

Primary: remote Ollama large model (e.g. Qwen3‑14B or similar) reachable over the network (often via a Tailscale IP).

Fallback: deterministic offline intent parser (no LLM) for core basic commands when the remote Ollama endpoint is unavailable.

Configuration changes implied:

Only a single OLLAMA_URL (or OLLAMA_BASE_URL) is required, representing the remote host, which may be a Tailnet IP (e.g. http://100.x.y.z:11434).

No need for separate OLLAMA_LAN_URL vs OLLAMA_LOCAL_URL or for per‑tier model names beyond “primary model name” (used when the remote endpoint is available).

Routing logic changes:

Health check remains a fast /api/tags probe to the configured OLLAMA_URL.

On health failure or timeout, the router should hand off directly to the deterministic offline parser instead of trying a second model.

All timeout and retry logic should be written with the assumption that LLM inference is always remote, not local: i.e. slightly higher latency, possibility of Tailscale/VPN outages, etc.

Remote Ollama via Tailscale – operational assumption

The design should assume that the Ollama server is not on the same host as Home Assistant, but reachable via a private network (Tailscale).

The agent is expected to talk to Ollama by:

Using a base URL that is either the Tailnet IP or a hostname resolved within the Tailnet (e.g. http://100.x.x.x:11434).

Not exposing any extra security or auth layer beyond what Ollama itself provides and the network boundary (Tailscale) — i.e. network‑level trust, no additional tokens.

From the agent’s perspective, Ollama is “just HTTP on a private address”; the main impact on planning is:

Be more explicit about handling connection failures/timeouts to a potentially flaky remote LLM.

Surface meaningful errors when the remote LLM is unreachable so the offline intent parser can be used and the user can see what happened.

Proactive vs simulate toggle – integration point

The habit learning component should expose predictions to the orchestrator in a way that is independent of whether actions are actually taken: e.g. predict_actions(context) -> List[(action, confidence, reason)].

The orchestrator should:

Always be able to request and log predicted actions.

Decide, based on the global proactive mode flag, whether to:

execute the actions (real HA service calls),

or log them as “simulated” without actual service calls.

The CLI and/or add‑on options schema should expose this mode so it can be changed without code changes, starting with “simulation” as the default.
