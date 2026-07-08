# End-to-end smoke tests

These exercise a **deployed** stack (unlike the unit tests, which live next to each module and mock AWS). They are skipped unless the required env vars point at real resources.

| File | Verifies | Requires |
|---|---|---|
| `test_webhook_smoke.py` | Router accepts a signed Lark event and 200s fast; url_verification challenge echoes. | `WEBHOOK_URL`, `LARK_ENCRYPT_KEY` |

Run after `scripts/deploy.sh`:

```bash
WEBHOOK_URL=... LARK_ENCRYPT_KEY=... \
  uv run --with cryptography --with pytest python -m pytest tests/test_webhook_smoke.py -v
```
