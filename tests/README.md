# End-to-end smoke tests

These exercise a **deployed** stack (unlike the unit tests, which live next to
each module and mock AWS). They are skipped unless the required env vars point at
real resources.

| File | Verifies | Requires |
|---|---|---|
| `test_wss_bridge.py` | The critical assumption: AgentCore auto-bridges a presigned WSS connection to the agent's port 18789, and the agent streams chat deltas. | `WS_URL` (from `POST /api/session`) |
| `test_webhook_smoke.py` | Router accepts a signed Lark event and 200s fast; url_verification challenge echoes. | `WEBHOOK_URL`, `LARK_ENCRYPT_KEY` |

Run after `scripts/deploy.sh`:

```bash
# WSS bridge (the one design risk we must confirm)
WS_URL="$(curl -s -XPOST "$API/api/session" -H "Authorization: Bearer $JWT" | jq -r .wsUrl)" \
  uv run --with websockets --with pytest python -m pytest tests/test_wss_bridge.py -v

# webhook
WEBHOOK_URL=... LARK_ENCRYPT_KEY=... \
  uv run --with cryptography --with pytest python -m pytest tests/test_webhook_smoke.py -v
```

If `test_wss_bridge.py` fails to connect, the fallback is Lambda Function URL +
SSE — a server-side change; the SPA already renders streamed deltas.
