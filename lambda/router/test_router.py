"""Router unit tests — Lark webhook crypto + event handling.

Run: uv run --with cryptography --with boto3 python -m pytest lambda/router/test_router.py -v

Env is set before importing modules so boto3 clients construct without error;
AWS calls are mocked.
"""

import base64
import hashlib
import json
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("IDENTITY_TABLE_NAME", "test-identity")
os.environ.setdefault("LARK_SECRET_ID", "test/lark")
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x")

sys.path.insert(0, os.path.dirname(__file__))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

ENCRYPT_KEY = "test-encrypt-key"


def _lark_encrypt(plaintext: str, key_str: str = ENCRYPT_KEY) -> str:
    """Reproduce Lark's AES-256-CBC framing: sha256(key) key, IV prepended, PKCS#7."""
    key = hashlib.sha256(key_str.encode()).digest()
    iv = b"0123456789abcdef"
    data = plaintext.encode()
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(iv + ct).decode()


@pytest.fixture
def lark_mod():
    import lark
    lark._creds_cache = {"appId": "cli_x", "appSecret": "s",
                         "verificationToken": "v", "encryptKey": ENCRYPT_KEY}
    lark._token_cache = {"token": "", "expires_at": 0.0}
    return lark


def test_decrypt_event_roundtrip(lark_mod):
    payload = {"type": "url_verification", "challenge": "abc123"}
    enc = _lark_encrypt(json.dumps(payload))
    assert lark_mod.decrypt_event(enc) == payload


def test_verify_signature_valid(lark_mod):
    import time
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    nonce = "n1"
    sig = hashlib.sha256(f"{ts}{nonce}{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce,
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is True


def test_verify_signature_wrong_sig_fails(lark_mod):
    import time
    ts = str(int(time.time()))
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": "deadbeef"}
    assert lark_mod.verify_signature(headers, b"body") is False


def test_verify_signature_replay_window(lark_mod):
    old_ts = "1000000000"  # year 2001 — well outside window
    body = b"body"
    sig = hashlib.sha256(f"{old_ts}n{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": old_ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is False


def test_verify_signature_fail_closed_without_key(lark_mod):
    lark_mod._creds_cache = {"encryptKey": ""}
    assert lark_mod.verify_signature({"x-lark-signature": "x"}, b"b") is False


def test_send_message_chunks_long_text(lark_mod):
    calls = []

    class FakeResp:
        def __init__(self, d): self._d = d
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.data)
        return FakeResp({"code": 0})

    lark_mod._token_cache = {"token": "t", "expires_at": 9e18}
    with mock.patch.object(lark_mod.urllib.request, "urlopen", fake_urlopen):
        ok = lark_mod.send_message("oc_x", "A" * 45000)
    assert ok is True
    assert len(calls) == 3  # 45000 / 20000 -> 3 chunks


def test_challenge_regex_rejects_xss():
    import index
    assert index._CHALLENGE_RE.match("safe_challenge-1.2") is not None
    assert index._CHALLENGE_RE.match("<script>") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
