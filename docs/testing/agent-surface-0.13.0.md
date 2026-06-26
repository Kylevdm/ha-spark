# ha-spark 0.13.0 — Agent Surface: Step-by-Step Testing Guide

---

## Prerequisites

Before starting, confirm:

- [ ] You have a running Home Assistant instance reachable from your dev machine.
- [ ] The ha-spark daemon can connect to it (`python3 -m ha_spark health` — all checks green, or only Ollama/load-history warnings are acceptable).
- [ ] The Python venv is activated: `source .venv/bin/activate` in the repo root.
- [ ] `HA_URL` and `HA_TOKEN` are set in `.env` (or exported) for standalone mode.
- [ ] `git log --oneline -1` shows `4e262ce` (you're on the right master).

---

## Section 1 — Existing API Routes (Ingress)

These are the ported aiohttp routes that must still work exactly as before.

**1.1 — Start the daemon**

```bash
python3 -m ha_spark daemon
```

Leave it running. Run all curl commands below in a second terminal against port 8099.

**1.2 — Health check**

```bash
curl -s http://localhost:8099/api/health | python3 -m json.tool
```

Expected: `{"status": "ok", "plan_at": null}` (null until a plan has run).

**1.3 — Plan endpoint (before a run)**

```bash
curl -s http://localhost:8099/api/plan | python3 -m json.tool
```

Expected: `{"plan": null, "generated_at": null}`

**1.4 — Config read**

```bash
curl -s http://localhost:8099/api/config | python3 -m json.tool
```

Expected: JSON with all whitelisted config keys. Confirm `agent_surface` defaults to `"off"` and `agent_exposure` to `"read_act"`.

**1.5 — Secret masking in config**

If `octopus_api_key` or `agent_api_token` is set:

```bash
curl -s http://localhost:8099/api/config | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('octopus:', d.get('octopus_api_key'))
print('token:', d.get('agent_api_token'))
"
```

Expected: any set secret shows as `***`, not its real value. Unset secrets stay `""`.

**1.6 — Config write + hot-reload**

```bash
curl -s -X POST http://localhost:8099/api/config \
  -H "Content-Type: application/json" \
  -d '{"min_soc": 22.0}' | python3 -m json.tool
```

Expected: 200, response contains `"min_soc": 22.0`. Daemon log must NOT show a restart.

Reset:

```bash
curl -s -X POST http://localhost:8099/api/config \
  -H "Content-Type: application/json" \
  -d '{"min_soc": 20.0}'
```

**1.7 — Config write rejects non-object body**

```bash
curl -s -X POST http://localhost:8099/api/config \
  -H "Content-Type: application/json" \
  -d '[1,2,3]'
```

Expected: HTTP 400, body contains `"error"`.

---

## Section 2 — Agent Surface Config Options

Stop the daemon. These tests run in isolation (no daemon needed).

**2.1 — Defaults**

```bash
python3 -c "
from ha_spark.config import Settings
s = Settings(ha_url='http://ha.test', ha_token='x')
print('agent_surface:', s.agent_surface)        # off
print('agent_exposure:', s.agent_exposure)       # read_act
print('agent_expose_port:', s.agent_expose_port) # False
print('agent_api_token:', repr(s.agent_api_token)) # ''
"
```

**2.2 — Option keys whitelisted**

```bash
python3 -c "
from ha_spark.config import _OPTION_KEYS
for k in ('agent_surface','agent_exposure','agent_api_token','agent_expose_port'):
    print(k, 'in whitelist:', k in _OPTION_KEYS)
"
```

Expected: all four print `True`.

**2.3 — Secret keys defined**

```bash
python3 -c "
from ha_spark.config import _SECRET_OPTION_KEYS
print('secret keys:', _SECRET_OPTION_KEYS)
"
```

Expected: frozenset containing at least `octopus_api_key` and `agent_api_token`.

---

## Section 3 — Bearer-Token Auth Helper

**3.1 — Auto-generate token**

```bash
python3 -c "
import tempfile
from pathlib import Path
from ha_spark.agent.auth import resolve_token
from ha_spark.config import Settings
tmp = Path(tempfile.mkdtemp())
s = Settings(ha_url='http://ha.test', ha_token='x')
tok = resolve_token(s, tmp / 'agent_token')
print('token length:', len(tok), '(should be ~43 chars)')
print('file exists:', (tmp / 'agent_token').exists())
print('stable on 2nd call:', resolve_token(s, tmp / 'agent_token') == tok)
"
```

Expected: ~43 chars, file exists, second call returns same value.

**3.2 — Configured token wins**

```bash
python3 -c "
import tempfile
from pathlib import Path
from ha_spark.agent.auth import resolve_token
from ha_spark.config import Settings
s = Settings(ha_url='http://ha.test', ha_token='x', agent_api_token='my-secret')
tok = resolve_token(s, Path(tempfile.mkdtemp()) / 'agent_token')
print('token:', tok)  # should be my-secret
"
```

**3.3 — Verify helper**

```bash
python3 -c "
from ha_spark.agent.auth import verify
print(verify('Bearer abc', 'abc'))    # True
print(verify('Bearer wrong', 'abc'))  # False
print(verify(None, 'abc'))            # False
print(verify('abc', 'abc'))           # False (missing Bearer prefix)
"
```

---

## Section 4 — Exposure Gating (no daemon needed)

```bash
python3 << 'EOF'
import json
from pathlib import Path
import tempfile
from fastapi.testclient import TestClient
from ha_spark.api.server import AppState, build_app
from ha_spark.config import Settings

tmp = Path(tempfile.mkdtemp())

def client(exposure):
    opts = tmp / f"opts_{exposure}.json"
    s = Settings(ha_url="http://ha.test", ha_token="x",
                 db_path=str(tmp / "t.db"), agent_exposure=exposure)
    state = AppState(settings=s, options_path=opts,
                     reload=lambda: Settings(**json.loads(opts.read_text())))
    return TestClient(build_app(state))

c = client("read")
print("read /agent/health:", c.get("/agent/health").status_code)        # 200
print("read POST /agent/run:", c.post("/agent/run").status_code)         # 404
print("read POST /agent/config:", c.post("/agent/config", json={}).status_code)  # 404

c = client("read_act")
print("read_act POST /agent/run:", c.post("/agent/run").status_code)     # NOT 404
print("read_act POST /agent/config:", c.post("/agent/config", json={}).status_code)  # 404

c = client("read_write")
print("read_write POST /agent/config:", c.post("/agent/config", json={"min_soc": 30.0}).status_code)  # 200
EOF
```

---

## Section 5 — Live Agent Routes (Daemon Running)

Restart daemon. Use port 8099.

**5.1 — GET /agent/health**

```bash
curl -s http://localhost:8099/agent/health | python3 -m json.tool
```

Expected: `{"checks": [...]}` — list of health check results.

**5.2 — GET /agent/context (empty)**

```bash
curl -s http://localhost:8099/agent/context | python3 -m json.tool
```

Expected: `{"facts": []}`

**5.3 — POST /agent/context (add fact)**

```bash
curl -s -X POST http://localhost:8099/agent/context \
  -H "Content-Type: application/json" \
  -d '{"kind": "away", "start_date": "2026-07-01", "end_date": "2026-07-07", "note": "test holiday"}' \
  | python3 -m json.tool
```

Expected: 200, `facts` array contains one entry with `"kind": "away"`.

**5.4 — GET /agent/context (read back)**

```bash
curl -s http://localhost:8099/agent/context | python3 -m json.tool
```

Expected: same fact visible.

**5.5 — GET /agent/plan**

```bash
curl -s http://localhost:8099/agent/plan | python3 -m json.tool
```

Expected: 200 with a `plan` array of HA sensor entities and a `generated_at` timestamp.

**5.6 — Secrets absent from agent routes**

Verify `agent_api_token` and `octopus_api_key` appear as `***` when set, never plaintext.

---

## Section 6 — OpenAPI Schema

With daemon running:

```bash
curl -s http://localhost:8099/openapi.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('title:', d.get('info',{}).get('title'))
paths = list(d.get('paths',{}).keys())
print('agent paths:', [p for p in paths if '/agent' in p])
"
```

Expected: title `ha-spark`, `/agent/plan`, `/agent/health`, `/agent/context` listed.

**Security check — no secrets in schema:**

```bash
curl -s http://localhost:8099/openapi.json | grep -i "api_token\|octopus_api_key\|SUPERVISOR\|HA_TOKEN"
```

Expected: no matches.

---

## Section 7 — MCP Surface

**7.1 — Initialize**

```bash
curl -s -X POST http://localhost:8099/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
      "protocolVersion": "2025-06-18",
      "capabilities": {},
      "clientInfo": {"name": "test", "version": "0"}
    }
  }'
```

Expected: HTTP 200, `mcp-session-id` response header present, JSON-RPC result body.

**7.2 — List tools** (replace `<SESSION_ID>` with the header value from 7.1):

```bash
curl -s -X POST http://localhost:8099/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: <SESSION_ID>" \
  -d '{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}'
```

Expected: tools listed. At `read_act`: `get_plan`, `get_state`, `get_forecast`, `get_predictions`, `get_health`, `get_context`, `add_context`, `run_plan` present. `set_config` **absent** (only in `read_write`).

**7.3 — Claude Desktop (optional)**

Add to Claude Desktop MCP config:

```json
{"mcpServers": {"ha-spark": {"url": "http://localhost:8099/mcp"}}}
```

Confirm ha-spark tools appear and `get_health` returns real data without secrets.

---

## Section 8 — Published Port + Token Gate

Requires `agent_surface: on` and `agent_expose_port: true`. Enable via hot-reload, then **restart the daemon** (port binding is at startup).

```bash
curl -s -X POST http://localhost:8099/api/config \
  -H "Content-Type: application/json" \
  -d '{"agent_surface": "on", "agent_expose_port": true}'
```

**8.1 — No auth → 401**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8098/api/health
```

Expected: `401`

**8.2 — Wrong token → 401**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8098/api/health \
  -H "Authorization: Bearer wrong-token"
```

Expected: `401`

**8.3 — Correct token → 200**

```bash
TOKEN=$(cat /data/agent_token 2>/dev/null || echo "your-configured-token")
curl -s http://localhost:8098/api/health \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Expected: `200`, `{"status": "ok", ...}`

**8.4 — Token gate covers /mcp**

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://localhost:8098/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}'
```

Expected: `401` without auth header, `200` with `-H "Authorization: Bearer $TOKEN"`.

**8.5 — Token never logged**

Check daemon log. Must say `Generated agent API token (saved to /data/agent_token)` — **not** the token value itself.

---

## Section 9 — PROACTIVE_MODE Gate

**9.1 — run_plan in simulate mode**

With default `proactive_mode: simulate`, POST:

```bash
curl -s -X POST http://localhost:8099/agent/run | python3 -m json.tool
```

Expected: 200 with plan result. Daemon log must show `[simulate]` lines, NOT actual `call_service` calls.

**9.2 — Confirm via daemon log**

Search for `Would call service` or `simulate:` — confirms planner ran without actuating hardware.

---

## Section 10 — Packaging Checks (no daemon needed)

**10.1 — Version**

```bash
grep "^version:" ha_spark_addon/config.yaml
```

Expected: `version: "0.13.0"`

**10.2 — Port declared**

```bash
grep -A2 "^ports:" ha_spark_addon/config.yaml
```

Expected: `8098/tcp: null`

**10.3 — Agent keys in schema**

```bash
grep "agent_" ha_spark_addon/config.yaml
```

Expected: `agent_surface`, `agent_exposure`, `agent_api_token`, `agent_expose_port` in both `options:` and `schema:` sections.

**10.4 — Schema sync test**

```bash
pytest tests/test_config.py -q
```

Expected: all pass.

**10.5 — Full suite**

```bash
ruff check . && mypy ha_spark && pytest -q
```

Expected: all three gates green.

---

## Test Plan — Record Sheet

Fill in **Actual Result** and **Pass/Fail**, then return this table for review.

| ID | Area | Test Description | Expected | Actual | Pass/Fail | Notes |
|---|---|---|---|---|---|---|
| 1.2 | Ingress API | `GET /api/health` | `{"status":"ok",...}` 200 | | | |
| 1.3 | Ingress API | `GET /api/plan` (no plan) | `{"plan":null,...}` 200 | | | |
| 1.4 | Ingress API | `GET /api/config` | JSON with `agent_surface:"off"` | | | |
| 1.5 | Security | Secret masked in config | `***` when set, `""` when unset | | | |
| 1.6 | Hot-reload | `POST /api/config {"min_soc":22}` | 200, updated, no restart | | | |
| 1.7 | Validation | `POST /api/config` with array | 400 | | | |
| 2.1 | Config | Agent option defaults | `off`, `read_act`, `False`, `""` | | | |
| 2.2 | Config | Agent keys in `_OPTION_KEYS` | All 4 print `True` | | | |
| 2.3 | Config | `_SECRET_OPTION_KEYS` | Frozenset with both secret keys | | | |
| 3.1 | Auth | Token auto-generated + persisted | ~43 chars, stable, file exists | | | |
| 3.2 | Auth | Configured token wins | Returns `my-secret` | | | |
| 3.3 | Auth | `verify()` helper | True/False/False/False | | | |
| 4.1 | Gating | `read` mode: act routes absent | `/agent/run` → 404 | | | |
| 4.2 | Gating | `read_act` mode: act route present | `/agent/run` → NOT 404 | | | |
| 4.3 | Gating | `read_write`: config route present | `/agent/config` POST → 200 | | | |
| 5.1 | Live routes | `GET /agent/health` | 200 with checks list | | | |
| 5.2 | Live routes | `GET /agent/context` empty | `{"facts":[]}` | | | |
| 5.3 | Live routes | `POST /agent/context` | 200, fact in response | | | |
| 5.4 | Live routes | `GET /agent/context` read back | Same fact visible | | | |
| 5.5 | Live routes | `GET /agent/plan` | 200 with plan + `generated_at` | | | |
| 5.6 | Security | Secrets absent from agent routes | Never plaintext | | | |
| 6.1 | OpenAPI | Schema title + agent paths | title `ha-spark`, paths listed | | | |
| 6.2 | Security | Secrets absent from OpenAPI | `grep` finds nothing | | | |
| 7.1 | MCP | `POST /mcp` initialize | 200 + `mcp-session-id` header | | | |
| 7.2 | MCP | `tools/list` | Read tools listed; `set_config` absent | | | |
| 7.3 | MCP | Claude Desktop (optional) | Tools appear, `get_health` works | | | |
| 8.1 | Port/Token | No auth on port 8098 | 401 | | | |
| 8.2 | Port/Token | Wrong token | 401 | | | |
| 8.3 | Port/Token | Correct token | 200 | | | |
| 8.4 | Port/Token | Token gate covers `/mcp` | 401 without, 200 with token | | | |
| 8.5 | Security | Token value never in log | Log shows path, not value | | | |
| 9.1 | PROACTIVE | `run_plan` in simulate mode | 200 plan; no real HA calls | | | |
| 9.2 | PROACTIVE | Daemon log shows simulate lines | `simulate:` entries visible | | | |
| 10.1 | Packaging | Version in config.yaml | `"0.13.0"` | | | |
| 10.2 | Packaging | Port 8098 declared | `8098/tcp: null` | | | |
| 10.3 | Packaging | Agent keys in schema + options | All 4 in both sections | | | |
| 10.4 | Packaging | Schema sync test | `pytest tests/test_config.py -q` passes | | | |
| 10.5 | Packaging | Full suite | ruff + mypy + pytest all green | | | |
