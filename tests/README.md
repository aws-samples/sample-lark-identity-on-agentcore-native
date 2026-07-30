# Tests

Two layers, deliberately separate.

## Unit tests — `tests/run.sh`

```bash
tests/run.sh
```

No AWS needed; everything is mocked. The tests themselves live next to the code they cover (`agent/test_agent.py`, `lambda/router/test_router.py`, `lambda/shim/test_shim.py`) and `run.sh` walks them, one process per suite: the router and shim dirs both define `index.py`, and the agent and router both define `identity.py`, so a single pytest session would import the wrong module for one of them.

Two constraints shape what these can cover:

- `agent_core` and `websearch` import `strands`/`mcp`, whose wheels target the ARM64 runtime and don't install on a typical x86 test host. Logic from those modules is either exec'd in isolation (see `_load_busy_helpers`) or asserted against the source text.
- Handlers that are mostly a sequence of AWS calls (`/reset`, `/new`, `/reconnect`, `/clear`) are left to the e2e path — mocking them would largely assert the mocks. The parts that actually carry risk are covered directly instead: Memory-thread rotation, conversational-only event counting, and the IdP registry's fallback.

## E2E smoke tests — need a deployed stack

These hit real resources and skip themselves unless the required env vars are set, so they're safe to leave alongside the unit runner.

| File | Verifies | Requires |
|---|---|---|
| `test_webhook_smoke.py` | Router accepts a signed Lark event and 200s fast; url_verification challenge echoes. | `WEBHOOK_URL`, `LARK_ENCRYPT_KEY` |

Run after `./deploy.sh`:

```bash
WEBHOOK_URL=... LARK_ENCRYPT_KEY=... \
  uv run --with cryptography --with pytest python -m pytest tests/test_webhook_smoke.py -v
```
