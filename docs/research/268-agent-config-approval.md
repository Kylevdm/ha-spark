# Trusted approval for agent config edits

Date: 2026-10-11
Ticket: [#268](https://github.com/Kylevdm/ha-spark/issues/268), part of map [#247](https://github.com/Kylevdm/ha-spark/issues/247).
Input decision: [#253](https://github.com/Kylevdm/ha-spark/issues/253#issuecomment-6103174886) /
[ADR-0006](../adr/0006-configuration-ownership-and-agent-edits.md).

Question: ADR-0006 allows an owner-allowlisted agent config edit only after a
trusted, one-use approval of the exact diff in the agent conversation. Which
agent clients and protocols can carry that approval to ha-spark so the server
can bind it to the authenticated owner, the proposed values, the config
revision and a single use? What happens with stateless HTTP and with generic
clients? What is the minimum interoperable contract, and how does it fail
closed?

All web sources were read on 2026-10-11. The current MCP revision is
**2026-07-28** ([versioning](https://modelcontextprotocol.io/specification/versioning)).
Source code is cited at pinned commits: Supervisor `8f21f7a3`, HA Core
`1df3b779`, `modelcontextprotocol/ext-apps` `82221c0c`. ha-spark is cited at
`cb0077b` (master). Full list under [Sources](#sources).

## TL;DR

- **No agent protocol or client today delivers a human approval that the
  server can verify.** Every in-client mechanism ends with the server receiving
  either an ordinary `tools/call` or an `accept` it cannot tell apart from a
  script. That covers MCP tool confirmations, MCP form-mode elicitation, ChatGPT
  write confirmations, Claude permission prompts, OpenAI `require_approval` /
  `mcp_approval_response`, Agents SDK `needs_approval` and MCP Apps buttons.
  The vendors' own docs say so. Claude Code documents a hook that
  "auto-respond[s] to elicitation requests without showing a dialog". ChatGPT
  widget tool calls "mirror model-initiated calls". MCP says servers "MUST NOT
  rely on client-provided user identification".
- **The one path the server can verify runs outside the conversation, on a
  surface ha-spark serves and HA authenticates: the ingress page.** The
  Supervisor's ingress proxy sets `X-Remote-User-Id` from the HA-authenticated
  session and strips any copy the client sent. **MCP URL-mode elicitation** is
  the standard way to send the user there from inside a conversation. Under
  2026-07-28 it rides on **Multi Round-Trip Requests (MRTR)**, which work over
  stateless HTTP.
- **Design consequence: the agent can only *propose*. The owner *approves*,
  and that approval *commits*, on ingress.** ha-spark keeps an immutable
  server-side proposal holding the diff, a digest, the base revision, an expiry
  and its state. The owner is identified from the ingress header. The commit
  checks the revision under the config write lock and consumes the proposal in
  the same step. The agent never holds anything it could redeem for a write.
- **Minimum interoperable contract:** `set_config` returns a plain,
  non-error "approval required" result with a proposal id, and a read-tier
  status tool reports the outcome. That works for every client: stateless HTTP,
  OpenAPI tool servers, HA Assist, the Responses API and the Messages API
  connector. URL elicitation through MRTR is an optional enhancement for MCP
  clients that declare `elicitation.url`.
- **Fail closed:** nothing commits except an owner click on that exact
  proposal on the ingress listener, with an unchanged revision, within its
  expiry, once.
- **Wording consequence for ADR-0006:** "approval in the agent conversation"
  is achievable as *requested and reported in the conversation, confirmed on
  ingress*. Only ha-spark's own ingress chat can keep the confirmation literally
  in the conversation, because there the conversation is the trusted surface.

## 1. What ha-spark has today

- **Agent surface.** `/mcp` is a FastMCP server over Streamable HTTP with
  `stateless_http=True` (`ha_spark/agent/mcp_server.py:65-76`). `/agent/*`
  exposes the same tool core as OpenAPI routes (`ha_spark/api/server.py:250-309`).
  The pin is `mcp>=1.2,<2` (`pyproject.toml`). The installed 1.30.0 speaks up to
  protocol 2025-11-25 (`mcp/types.py: LATEST_PROTOCOL_VERSION`).
- **Authentication.** Ingress requests are peer-gated to the Supervisor's
  ingress proxy (`api/server.py:8-14`). The published port 8098 uses **one
  shared static bearer token** (`ha_spark/agent/auth.py`). That token
  identifies "whoever holds the agent token", not a person. Claude's connector
  docs make the same point about static credentials: "don't use it to identify
  which user is calling"
  ([Claude auth](https://claude.com/docs/connectors/building/authentication)).
- **`set_config` writes directly today:** `state.apply_options(updates)` at
  exposure `read_write` (`mcp_server.py:129-132`). That conflicts with ADR-0006,
  as expected, because the ADR is a planning contract.
- **Planned ingress chat** ([#254 decision](https://github.com/Kylevdm/ha-spark/issues/254#issuecomment-6111055783)):
  SSE-framed JSON events, served on the ingress listener only, mutations
  require JSON plus a custom header, strict CSP with `frame-ancestors 'self'`.
- **Clients named in `ha_spark_addon/DOCS.md`:** open-webui (OpenAPI tool
  server), Claude Desktop and claude.ai (MCP Streamable HTTP connector).

## 2. Mechanisms: who approves, and what the server sees

| Mechanism | Where the human acts | What reaches ha-spark | Server-verifiable? |
|---|---|---|---|
| MCP tool-call confirmation (spec: there "SHOULD always be a human in the loop") | Client UI | An ordinary `tools/call` | **No** |
| MCP tool annotations (`readOnlyHint`, `destructiveHint`, …) | n/a (a hint that steers the client's prompt) | Nothing | **No**. "Clients MUST consider tool annotations to be untrusted" |
| MCP **form**-mode elicitation | Client dialog | `{"action":"accept","content":{…}}` in `inputResponses` | **No** (client-asserted) |
| MCP **URL**-mode elicitation | Browser, on a URL the server chose | `accept` means only "user consented to open the URL"; the outcome is learned server-side | **Yes, if the page authenticates the user** |
| MCP authorization (OAuth 2.1, step-up `insufficient_scope`) | Authorization server login | A token carrying `sub` and coarse scopes | Identifies a user. **Not per-diff** |
| MCP Apps (`ui://` views, `visibility: ["app"]`) | Sandboxed iframe in the host | A `tools/call` relayed by the host | **No** (host-trusted) |
| OpenAI Responses API `require_approval` | Developer's app | A plain `tools/call` from OpenAI after `mcp_approval_response {approve:true}` | **No** |
| OpenAI Agents SDK `needs_approval` / `on_approval_request` | App code | A plain call | **No** |
| ChatGPT connectors and apps | ChatGPT confirmation UI | A plain call. `_meta` ids are "hints only" | **No** |
| Claude Code permission prompt, `anthropic/requiresUserInteraction` | Claude Code dialog | A plain call | **No** |
| claude.ai / Desktop / mobile connectors | Claude app ("Always allow" option) | A plain call, from Anthropic's cloud | **No** |
| Claude Messages API MCP connector | n/a | Tool calls only | **No** |
| HA MCP client integration (Assist) | n/a | Tool calls only | **No** |
| Open WebUI native MCP | Client dialog (form) or link (URL) | Same as MCP rows above | Form: no. URL: yes |
| **ha-spark ingress page or ingress chat** | HA-authenticated browser | An owner-authenticated POST via the ingress proxy | **Yes** |

Evidence per row:

- **MCP tools.** The spec says "there SHOULD always be a human in the loop
  with the ability to deny tool invocations" and that applications "SHOULD …
  Present confirmation prompts". Both are client duties. The server's duties are
  to "Validate all tool inputs" and "Implement proper access controls"
  ([tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)).
  On annotations: "clients MUST consider tool annotations to be untrusted unless
  they come from trusted servers" (same page). The schema's `ToolAnnotations`
  fields are all `…Hint` booleans
  ([schema.ts](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/1f520964b295d58494d944f2aea8a5ab51c79f77/schema/2026-07-28/schema.ts)).
- **Form elicitation.** The `ElicitResult` doc comment reads "`accept`: User
  submitted the form/confirmed the action". It is the client's statement.
  "Servers MUST NOT rely on client-provided user identification without server
  verification, as this can be forged"
  ([elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)).
  Claude Code ships a programmatic path: "To auto-respond to elicitation
  requests without showing a dialog, use the Elicitation hook"
  ([Claude Code MCP](https://code.claude.com/docs/en/mcp#respond-to-mcp-elicitation-requests)).
  Its hook output takes `action` (accept/decline/cancel) and `content`
  ([hooks](https://code.claude.com/docs/en/hooks)).
- **URL elicitation.** The interaction "occurs out of band and the client is
  not directly informed of the outcome. When the client retries the original
  request, the server determines from the echoed `requestState` (or its own
  stored state) whether the out-of-band interaction has completed." For
  phishing, "The MCP Server MUST verify the identity of the user who opens the
  URL", and the server "MUST NOT provide a URL which is pre-authenticated"
  ([elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)).
- **MCP authorization.** It is OPTIONAL, and the MCP server is an OAuth 2.1
  resource server that "MUST validate that access tokens were issued
  specifically for them as the intended audience" (RFC 8707). Step-up uses
  `403 insufficient_scope` with a `scope` challenge
  ([authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)).
  A scope names a permission class, not a specific diff. HA cannot serve as
  ha-spark's authorization server under this rule: HA tokens are minted for HA,
  so accepting them would break audience binding and amount to the
  token-passthrough pattern the spec forbids.
- **MCP Apps.** The host "MUST NOT include tools in the agent's tool list when
  their visibility does not include `"model"`" and "MUST reject `tools/call`
  requests from apps for tools that don't include `"app"`". Both are host
  obligations. The host "MAY forward any message from the View … to the MCP
  Apps server"
  ([ext-apps spec 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/82221c0c8ce7661efa6771c9d461511b1650495f/specification/2026-01-26/apps.mdx),
  lines 395-401, 487). OpenAI's reference describes `window.openai.callTool` as
  "Invoke another MCP tool from the widget (mirrors model-initiated calls)" and
  says the `_meta` fields "are hints only; servers should never rely on them for
  authorization decisions"
  ([Apps SDK reference](https://developers.openai.com/apps-sdk/reference)).
- **OpenAI Responses API.** "A request for an approval to make an MCP tool call
  creates a `mcp_approval_request` item". The developer answers with an
  `mcp_approval_response` (`approve`, `approval_request_id`). "Each approval
  applies to one tool call". `require_approval` accepts `"always"`, `"never"` or
  a per-tool filter. The page doesn't say the MCP server is told anything about
  approval
  ([MCP tools guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)).
- **OpenAI Agents SDK.** `needs_approval` pauses the run into `interruptions`,
  and `state.approve()` / `state.reject()` resume it. Hosted MCP
  `on_approval_request` callbacks let "the run continue without pausing for a
  human response". `RunState.from_json()` / `from_string()` "do not authenticate
  the snapshot or the person submitting it". The page's guidance for
  long-running approvals is the same shape this doc recommends: authenticate
  the reviewer through your own session, not the request body; accept decisions
  only against server-stored pending items; and use storage-level coordination
  so a replayed submission can't resume twice
  ([human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/),
  [MCP](https://openai.github.io/openai-agents-python/mcp/)).
- **ChatGPT.** "Write actions by default require confirmation." "You can choose
  to remember the approve or deny choice for a given tool for a conversation"
  ([custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server)).
  Annotations "do not replace authorization, validation, or confirmation in your
  server" ([build an MCP server](https://developers.openai.com/plugins/build/mcp-server)).
- **Claude.** `_meta["anthropic/requiresUserInteraction"]: true` forces Claude
  Code's prompt on every call. However, "The Agent SDK's `canUseTool` callback
  does receive these calls and can approve them"
  ([Claude Code MCP](https://code.claude.com/docs/en/mcp#require-approval-for-a-specific-tool)).
  Claude Code's v2 runtime declares `elicitation: {form: {}, url: {}}` on
  2026-07-28 connections (same page). Hosted Claude apps connect from Anthropic's
  cloud. Static-header auth is "Beta, for a limited set of organizations"
  ([Claude auth](https://claude.com/docs/connectors/building/authentication)),
  and users are told "Only click **Always allow** for trusted servers"
  ([custom connector](https://claude.com/docs/connectors/custom/remote-mcp)).
  For the Messages API connector, "only tool calls are currently supported"
  ([MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)).
- **Home Assistant.** The MCP integration makes HA a client "using the
  Server-Sent Events (SSE) transport". It supports tools, but not prompts,
  resources, sampling or notifications, and the docs mention no confirmations
  ([HA MCP](https://www.home-assistant.io/integrations/mcp/)).
- **Open WebUI.** Native MCP is "Streamable HTTP only". Its elicitation shows
  form requests and link requests. OpenAPI tool servers remain "the preferred
  integration path", and they have no elicitation
  ([Open WebUI MCP](https://docs.openwebui.com/features/extensibility/mcp/)).
- **Ingress identity.** Supervisor `api/ingress.py:_init_header` (lines
  333-358) sets `X-Remote-User-Id`, `X-Remote-User-Name` and
  `X-Remote-User-Display-Name` from the session's user, and drops any of those
  headers arriving from the client
  ([source](https://github.com/home-assistant/supervisor/blob/8f21f7a3fe40c8ff34b428f52194af88ed5cfc32/supervisor/api/ingress.py#L333-L358)).
  HA Core sends the WebSocket-authenticated user's id when it creates the
  session: "Send user ID on session creation, so the supervisor can correlate
  session tokens with users"
  ([`hassio/websocket_api.py:135-139`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/components/hassio/websocket_api.py#L135-L139)).
  The add-on docs require that "Only connections from `172.30.32.2` must be
  allowed"
  ([presentation](https://developers.home-assistant.io/docs/add-ons/presentation)).

## 3. Binding to owner, values, revision and one use

No mechanism binds all four natively. MCP supplies the pieces and leaves the
binding to the server. For `requestState`, servers "MUST protect its
integrity", "SHOULD include … the authenticated principal … a short expiry …
an identifier for the originating request", and the spec adds: "these measures
… do not by themselves guarantee single-use. Servers for which a given
`requestState` must be consumed at most once … MUST enforce that invariant
server-side"
([MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr)).
Elicitation adds "Servers MUST bind elicitation requests to the client and user
identity". ha-spark adds the binding on its own side:

| Binding | How ha-spark provides it |
|---|---|
| **Proposal** | The server stores an immutable record: random 128-bit id; agent principal (the published-port token holder, or the ingress user for ingress chat); base revision of the owning store; canonical diff (effective before/after per field, masked by the ADR-0006 secret path); SHA-256 digest over canonical JSON of `{id, base_revision, diff}`; created and expiry times; state `pending → committed / declined / expired / stale / failed / superseded` |
| **Owner** | Approval is accepted only on the **ingress listener**, which is already peer-gated to the ingress proxy, and only when `X-Remote-User-Id` equals the owner's HA user id recorded by ha-spark. On the published port that header would be client-controlled, so approval routes must not exist there. #254 already keeps UI routes off that port |
| **Values** | The approval page renders the diff from the stored record, never from agent text. The approve POST carries the proposal id **and the digest the page displayed**. A mismatch is rejected |
| **Revision** | The commit runs through the shared ADR-0006 mutation service with expected revision = the proposal's base revision, under the config lock. If the revision changed, nothing is written, the proposal becomes `stale`, and the agent must re-propose |
| **One use** | **Approve = commit.** The `pending → committed` transition happens inside the same lock as the write. The agent never receives a redeemable "approved" token, so there is no replay window. A second click is a no-op |
| **Expiry** | Short TTL (e.g. 15 min, tunable). Keeping pending proposals in memory means a restart expires them, which fails closed |
| **Allowlist / validation** | Re-checked at commit time (ADR-0006), so an allowlist narrowed after the proposal still blocks it |

Options considered and rejected as the trust anchor:

- **Form elicitation, a "type YES" field, or the agent echoing a server
  nonce.** The client, a hook or the model can produce any of these. A nonce
  that passes through the model proves nothing.
- **A one-time code shown on ingress (or in a notification) and typed back
  into the conversation.** This is verifiable, but it adds steps to "click
  Approve on the page that showed the code". It could be offered later for
  literal "in-conversation" approval, but it is not part of the minimum.
- **An HA companion-app actionable notification as the approval.** HA's REST
  `POST /api/events/<type>` is `@require_admin` and fires with the calling
  token's context
  ([`api/__init__.py` `APIEventView.post`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/components/api/__init__.py)).
  An LLM with any owner-scoped HA tool server could therefore forge the action
  event. Use notifications only to *alert* the owner, with a link to ingress.
- **OAuth step-up or per-diff scopes.** This needs an ha-spark authorization
  server, and HA cannot be one (audience binding, see §2). A scope does not
  carry a diff, and the flow is still a browser detour, heavier than ingress for
  no gain. OAuth could later replace the shared agent token for *caller*
  identity. That is out of scope here.
- **MCP Apps approval button.** It is host-relayed and indistinguishable from
  a model call (§2).

## 4. Stateless HTTP, legacy and generic clients

- **2026-07-28 clients.** Sessions and server-initiated requests are gone:
  "Servers MUST send server-to-client requests (such as … `elicitation/create`)
  using the MRTR pattern." A tool returns `InputRequiredResult`
  (`resultType: "input_required"`, `inputRequests`, `requestState`), and the
  client retries the original `tools/call` with `inputResponses` and the
  echoed `requestState`. "The server processing the retry does not need any
  information beyond what is directly present in the retry request"
  ([MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr);
  [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog) items 1, 2, 7, 11).
  This fits ha-spark's stateless design. Constraints: "Servers MUST NOT send an
  `inputRequests` that the client has not declared support for", and clients
  declare capabilities per request in `_meta`
  ([elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation)).
  "Servers MUST NOT assume that clients will fulfill the `inputRequests` or
  retry the original request" (MRTR).
- **2025-era clients (≤ 2025-11-25).** Elicitation is a server-initiated
  request on a session. ha-spark runs `stateless_http=True`, which "creates a
  completely fresh transport for each request with no session tracking"
  (`mcp/server/streamable_http_manager.py` docstring, SDK 1.30.0). The SDK v2
  notes say 2025-era clients "still open sessions and still need whatever
  stickiness they needed on v1"
  ([What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)).
  Turning sessions back on just for this isn't worth it: the fallback below
  covers these clients.
- **SDK.** MRTR needs the MCP Python SDK v2. v2.0.0 shipped 2026-07-28, and
  "v1.x is in maintenance mode and will only receive security fixes"
  ([v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)).
  v2 lets a tool return the question (`Resolve(fn)` / `Elicit`). Under
  2026-07-28 `ctx.elicit()` raises `NoBackChannelError`. The SDK "seals and
  verifies" `request_state` with a key "minted per process" by default
  ([What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)).
  For a single-process add-on that is enough: a restart invalidates in-flight
  state, which fails closed.
- **Generic clients.** These are the OpenAPI `/agent/config` route (open-webui
  tool servers), the Responses API, the Messages API connector and HA Assist.
  None can elicit. They get the plain "approval required" result and the status
  tool. That is the same contract, minus the redirect.

## 5. Minimum interoperable contract

Server-side requirements. Clients need nothing beyond calling a tool and
showing its text.

1. **Propose-only write.** `set_config(updates)` on MCP and `POST /agent/config`
   on OpenAPI keep the existing exposure gate (`read_write`). They run the
   ADR-0006 checks: allowlist; whole-document validation; no credentials, agent
   permissions or mode/authority keys. They then create a proposal and return a
   **non-error** structured result:
   `{status: "approval_required", proposal_id, base_revision, diff, digest, expires_at, approve_hint}`.
   `approve_hint` is fixed server text such as "Open ha-spark in Home Assistant
   → Approvals". The diff uses masked effective values. They **never apply
   anything**. Validation failures return a tool execution error naming the
   field.
2. **Outcome lookup.** A read-tier `get_config_proposal(proposal_id)` returns
   the state, plus the committed revision or the failure reason. Stateless and
   generic clients use it to learn the result.
3. **Approval surface (ingress only).** `GET /api/config/proposals` and
   `POST /api/config/proposals/{id}/approve|decline` follow the #254 rules:
   JSON plus a custom header, strict CSP, `frame-ancestors 'self'`. The routes
   require the ingress peer and `X-Remote-User-Id == owner_user_id`. The body
   carries `digest`. Approve commits through the shared mutation service with
   the expected revision and consumes the proposal in the same locked step
   (§3).
4. **Trusted rendering.** The ingress page and the ingress chat render
   proposals only from server records. In the chat, a dedicated SSE event type
   carries the proposal card, and model text never renders as a control. Any
   "likely consequence" text from the LLM is labelled as the agent's
   explanation, not shown as fact.
5. **MCP enhancement** (2026-07-28 clients declaring `elicitation.url`). On the
   first call, `set_config` returns `InputRequiredResult` with one URL-mode
   elicitation:
   - **URL:** the ingress approvals page, built from HA's `external_url` /
     `internal_url` (`GET /api/config` exposes both:
     [`core_config.py`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/core_config.py)).
     It carries only the opaque proposal id: no values, not pre-authenticated.
   - **Message:** written by the server, not by the model.
   - **`requestState`:** sealed `{proposal_id, digest, principal, tool, args_digest, exp}`.

   On the retry, ha-spark verifies the seal, principal, tool and arguments,
   then reports: `committed` → a complete result; `pending` → one more
   `InputRequiredResult` (bounded), then the plain `approval_required` result;
   `declined` / `expired` / `stale` / `failed` → an `isError` result with the
   reason. **A retry never commits.** The MCP `decline` / `cancel` actions mark
   the proposal declined.
6. **Owner identity.** ha-spark records the owner's HA user id, for example
   from the ingress user who turns agent edits on. If no owner is recorded, or
   in standalone/dev mode without ingress identity, agent config writes are
   unavailable.
7. **Hygiene.** Annotate truthfully. Proposal creation is additive
   (`readOnlyHint: false`, `destructiveHint: false`, `openWorldHint: false`).
   Client confirmations are a UX bonus, never the gate. Cap pending proposals:
   one per agent principal, and a new one supersedes the old. That limits
   approval fatigue and spam. Audit every proposal and outcome in SQLite
   history, without secrets.

```
agent ──set_config(updates)──▶ ha-spark: validate → proposal P (pending, rev r, digest d)
  ◀── approval_required{P} │ or InputRequiredResult{url: <HA>/…ingress…#P, requestState}
owner ──HA login/session──▶ ingress approvals page (diff from record) ──approve{P,d}──▶
        ha-spark: owner id ✓ ingress peer ✓ d ✓ rev==r ✓ allowlist ✓ validate ✓
                  → write + P:=committed (one locked step)
agent ──get_config_proposal(P) │ MRTR retry──▶ committed(rev r+1) / declined / stale / expired
```

## 6. Fail-closed behaviour

| Condition | Behaviour |
|---|---|
| Client has no elicitation, is 2025-era stateless, OpenAPI, HA Assist, Responses API or Messages API | `approval_required` result. Commit only via ingress |
| Form-mode `accept`, a chat "yes", an agent claim, OpenAI `approve:true`, a client-side confirmation | No effect on any proposal |
| URL-mode `accept` without the ingress click | Still `pending` |
| Approver isn't the owner, the header is missing, the request came over the published port, or the peer isn't the ingress proxy | 403. Proposal unchanged |
| Digest mismatch | Rejected. Proposal unchanged |
| Revision moved since the proposal | No write. `stale`. The agent must re-propose |
| Field left the allowlist, or validation fails at commit | No write. `failed`, with a field error |
| TTL passed, or the add-on restarted | `expired` |
| Second approve, or a concurrent approve | No second write |
| `requestState` tampered, from another principal, expired, or for another tool/arguments | Retry rejected |
| No owner recorded, or standalone mode | `set_config` returns an error saying agent edits are unavailable |
| `agent_surface: off` or exposure below `read_write` | Tool absent (existing gate) |
| Pending cap reached | New proposal supersedes the older one, which becomes `superseded` |

## 7. Findings that affect existing code or docs (flagged, not changed)

- `set_config` and `POST /agent/config` apply writes directly today. Under
  ADR-0006 they become propose-only, as in §5.
- `mcp_server.py:76` disables DNS-rebinding/Host protection. MCP 2026-07-28
  Streamable HTTP says "Servers MUST validate the `Origin` header … If the
  `Origin` header is present and invalid, servers MUST respond with HTTP 403"
  ([transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)).
  The published port should at least reject a present, foreign `Origin`.
- The pin `mcp>=1.2,<2` sits on a line that is in maintenance mode. Adopting
  MRTR needs v2. That dependency change needs its own justification.
- `DOCS.md` tells Claude Desktop users to point a connector at
  `http://<host>:8098/mcp` with the bearer token. Claude's docs say the hosted
  apps, Desktop included, connect from Anthropic's cloud, and that static-header
  auth is a limited beta. That advice may not work as written.

## Open questions

1. **Who is "the owner"?** Options are one HA user id recorded by ha-spark,
   HA's `is_owner` user, or any HA admin the owner nominates. This matters for
   multi-admin households.
2. **Deep links.** Does the HA frontend's ingress panel pass a path or
   fragment through to the add-on iframe, so that a URL elicitation lands on the
   specific proposal? Unverified. The fallback is a pending-approvals list on
   the landing page.
3. **Reachability.** URL elicitation only helps where the owner's browser can
   reach HA (`external_url`, Nabu Casa). How does an `http://` LAN URL behave in
   clients, given the spec says "SHOULD use HTTPS"?
4. **Integration-owned fields.** ADR-0006 allows device and fallback fields to
   be agent-editable. The commit must call the integration's validated command
   with the integration's revision. That revision concept belongs to the
   integration entity contract and to #266.
5. **Numbers.** Values for the TTL, the MRTR re-ask bound and the pending
   cap.
6. **Sequencing.** Ship the universal fallback first (no SDK change). Is the
   MCP SDK v2 migration for URL elicitation a separate, later ticket?

## Unverified

- Whether ChatGPT and the hosted Claude apps (claude.ai, Desktop, mobile)
  support MCP elicitation, especially URL mode on 2026-07-28. The pages fetched
  don't document it. OpenAI's developer-mode help article returned 403.
- That a legacy (≤ 2025-11-25) elicitation cannot round-trip on ha-spark's
  `stateless_http=True`. This is inferred from the SDK docstring and v2 notes,
  not tested.
- Whether HA's MCP client now speaks Streamable HTTP. Its docs name only SSE,
  and ha-spark serves only Streamable HTTP, so as documented HA Assist cannot
  connect to ha-spark's `/mcp` at all.
- How each client renders an `http://` URL elicitation, and whether HA's
  ingress panel passes a fragment through (open questions 2-3).

## Sources

Accessed 2026-10-11.

- MCP specification 2026-07-28:
  [versioning](https://modelcontextprotocol.io/specification/versioning),
  [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
  [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr),
  [elicitation](https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation),
  [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools),
  [authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
  [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http),
  [schema.ts @`1f520964`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/1f520964b295d58494d944f2aea8a5ab51c79f77/schema/2026-07-28/schema.ts)
- MCP Apps: [ext-apps spec 2026-01-26 @`82221c0c`](https://github.com/modelcontextprotocol/ext-apps/blob/82221c0c8ce7661efa6771c9d461511b1650495f/specification/2026-01-26/apps.mdx)
- MCP Python SDK: [v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0),
  [What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/), installed
  1.30.0 `mcp/server/streamable_http_manager.py`, `mcp/types.py`, [PyPI](https://pypi.org/project/mcp/)
- OpenAI: [Responses API MCP tools](https://developers.openai.com/api/docs/guides/tools-connectors-mcp),
  [Agents SDK human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/),
  [Agents SDK MCP](https://openai.github.io/openai-agents-python/mcp/),
  [custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server),
  [build an MCP server](https://developers.openai.com/plugins/build/mcp-server),
  [MCP overview](https://developers.openai.com/api/docs/mcp),
  [Apps SDK reference](https://developers.openai.com/apps-sdk/reference)
- Anthropic: [Claude Code MCP](https://code.claude.com/docs/en/mcp),
  [Claude Code hooks](https://code.claude.com/docs/en/hooks),
  [connector authentication](https://claude.com/docs/connectors/building/authentication),
  [custom connectors](https://claude.com/docs/connectors/custom/remote-mcp),
  [Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)
- Home Assistant: [MCP integration](https://www.home-assistant.io/integrations/mcp/),
  [add-on presentation / ingress](https://developers.home-assistant.io/docs/add-ons/presentation),
  Supervisor [`api/ingress.py` @`8f21f7a3`](https://github.com/home-assistant/supervisor/blob/8f21f7a3fe40c8ff34b428f52194af88ed5cfc32/supervisor/api/ingress.py),
  Core @`1df3b779`: [`hassio/websocket_api.py`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/components/hassio/websocket_api.py),
  [`api/__init__.py`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/components/api/__init__.py),
  [`core_config.py`](https://github.com/home-assistant/core/blob/1df3b779e8f6e3576c3c259450f63557dc705c74/homeassistant/core_config.py)
- Open WebUI: [MCP](https://docs.openwebui.com/features/extensibility/mcp/)
- This repo @`cb0077b`: `ha_spark/agent/{mcp_server,auth,exposure,tools}.py`,
  `ha_spark/api/server.py`, `ha_spark_addon/{config.yaml,DOCS.md}`,
  `docs/adr/0006-configuration-ownership-and-agent-edits.md`, `CONTEXT.md`
