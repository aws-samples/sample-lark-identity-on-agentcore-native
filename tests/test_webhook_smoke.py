"""E2E: Router webhook accepts a signed Lark event and 200s.

Skipped unless WEBHOOK_URL + LARK_ENCRYPT_KEY are set.
Run: WEBHOOK_URL=... LARK_ENCRYPT_KEY=... \
     uv run --with cryptography --with pytest python -m pytest tests/test_webhook_smoke.py -v
"""

import hashlib
import json
import os
import time
import urllib.request

import pytest

WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
ENCRYPT_KEY = os.environ.get("LARK_ENCRYPT_KEY")
pytestmark = pytest.mark.skipif(
    not (WEBHOOK_URL and ENCRYPT_KEY),
    reason="set WEBHOOK_URL and LARK_ENCRYPT_KEY",
)


def _post(body: bytes, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_url_verification_challenge_echoes():
    body = json.dumps({"type": "url_verification", "challenge": "smoketest123"}).encode()
    status, text = _post(body, {"Content-Type": "application/json"})
    assert status == 200
    assert "smoketest123" in text


def test_signed_event_accepted_fast():
    body = json.dumps({"schema": "2.0", "header": {"event_type": "im.message.receive_v1"},
                       "event": {}}).encode()
    ts, nonce = str(int(time.time())), "n1"
    sig = hashlib.sha256(f"{ts}{nonce}{ENCRYPT_KEY}".encode() + body).hexdigest()
    status, _ = _post(body, {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": ts,
        "X-Lark-Request-Nonce": nonce,
        "X-Lark-Signature": sig,
    })
    assert status == 200


def test_bad_signature_rejected():
    body = b'{"header":{"event_type":"im.message.receive_v1"}}'
    status, _ = _post(body, {
        "Content-Type": "application/json",
        "X-Lark-Request-Timestamp": str(int(time.time())),
        "X-Lark-Request-Nonce": "n",
        "X-Lark-Signature": "deadbeef",
    })
    assert status == 401
