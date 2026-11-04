# In-app assistant and chat providers

Status: accepted (2026-10-14), confirmed by the owner in [Chat UX: streaming, history, offline fallback, and pending proposals](https://github.com/Kylevdm/ha-spark/issues/310). This is a v1 planning contract, not implemented behavior. It amends the standing decision of a single remote Ollama tier.

## Decision

The ingress app's chat is a tool-calling **assistant**, not today's one-shot router. It is its own agent principal on the read < read_act < read_propose ladder ([#269](https://github.com/Kylevdm/ha-spark/issues/269#issuecomment-6132918848)), and defaults to `read_act`. An owner changes the level in Settings. It calls the same `agent/tools.py` core as the MCP server, so it can never reach a device write. The deterministic router stays as the offline tier and answers whenever the provider is unreachable or the agent fails part-way, in which case it replaces any partial answer.

The owner picks **one** chat provider:

- **Ollama**, at `OLLAMA_URL`, as today.
- **OpenAI-compatible**: a base URL, an API key and a model, called on `/chat/completions`. OpenCode Go is one configuration of it. When configured, the client sends a stable `x-opencode-session` (the conversation id) and its own user agent. Endpoints that only speak Anthropic `/messages`, OpenAI `/responses` or Google's API are not supported.

The OpenAI-compatible provider is off by default. The API key is a secret: stored with the add-on's secrets ([ADR-0006](0006-configuration-ownership-and-agent-edits.md)), marked `password`, and never logged or echoed. Settings states plainly that chat context (plan, battery level, household facts such as away dates) leaves the house, and shows the chosen model's retention class as owner-read text that ha-spark does not verify.

We rejected:

- **Ordered tiers (Ollama, then a cloud provider, then offline).** It adds latency and a second failure path to explain, for little gain over the owner choosing.
- **A hardcoded OpenCode Go provider.** The generic shape covers it and other OpenAI-compatible endpoints with no extra code.
- **Keeping the one-shot router.** It cannot make replan requests or config proposals, which #269 and #278 already assume the in-app chat does.

## Consequences

- ha-spark is no longer guaranteed to keep chat on the local network. "No cloud service" still holds: there is no ha-spark-hosted service, and the third-party provider is the owner's opt-in. When this ships, README's "contacts only your Ollama server" sentence and the chat paragraph change to name the opt-in provider.
- OpenCode Go is intended for coding-agent traffic. Using it for household chat may fall outside that, and it may stop working. That risk is the owner's. ha-spark makes no promise about it.
- Each turn rebuilds a fresh grounding snapshot as the system prompt, so answers never rest on conversation history alone. The snapshot outranks the history. Only user text and final assistant text are stored, per HA user, for 30 days. Tool results are never replayed.
- The Telegram phone surface stays on the one-shot router in v1.
