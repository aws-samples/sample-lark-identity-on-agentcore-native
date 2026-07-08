"""Lark (Feishu) API + webhook crypto helpers.

Covers the channel-specific bits: credential loading, tenant access token
caching, webhook signature verification (with replay window), AES-256-CBC event
decryption, message send, and image download.

Crypto uses the `cryptography` package (portable, readable) rather than ctypes
+ system libcrypto. `verificationToken` is loaded for completeness but the
signature check uses `encryptKey` per Lark's spec.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.request
import urllib.error

import boto3
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("router.lark")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_SECRET_ID = os.environ.get("LARK_SECRET_ID", "")
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")
# Reject webhook events whose timestamp is older than this (replay protection).
_REPLAY_WINDOW_SECONDS = int(os.environ.get("LARK_REPLAY_WINDOW_SECONDS", "300"))
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

_secrets = boto3.client("secretsmanager", region_name=_REGION)

# module-level caches (per warm container)
_creds_cache: dict | None = None
_token_cache = {"token": "", "expires_at": 0.0}


def get_credentials() -> tuple[str, str, str, str]:
    """Return (appId, appSecret, verificationToken, encryptKey). ('',...) if unset."""
    global _creds_cache
    if _creds_cache is None:
        if not _SECRET_ID:
            return ("", "", "", "")
        try:
            raw = _secrets.get_secret_value(SecretId=_SECRET_ID)["SecretString"]
            _creds_cache = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            log.error("failed to load Lark credentials: %s", e)
            _creds_cache = {}
    c = _creds_cache
    return (c.get("appId", ""), c.get("appSecret", ""),
            c.get("verificationToken", ""), c.get("encryptKey", ""))


# ----------------------------- webhook security -----------------------------

def verify_signature(headers: dict, body_bytes: bytes) -> bool:
    """Verify X-Lark-Signature and enforce the replay window. Fail-closed."""
    _, _, _, encrypt_key = get_credentials()
    if not encrypt_key:
        log.warning("no encryptKey configured — rejecting webhook (fail-closed)")
        return False

    h = {k.lower(): v for k, v in headers.items()}
    timestamp = h.get("x-lark-request-timestamp")
    nonce = h.get("x-lark-request-nonce")
    signature = h.get("x-lark-signature")
    if not (timestamp and nonce and signature):
        return False

    # Replay window
    try:
        if abs(time.time() - int(timestamp)) > _REPLAY_WINDOW_SECONDS:
            log.warning("webhook timestamp outside replay window")
            return False
    except ValueError:
        return False

    material = f"{timestamp}{nonce}{encrypt_key}".encode() + body_bytes
    expected = hashlib.sha256(material).hexdigest()
    return hashlib.sha256 and _consteq(expected, signature)


def _consteq(a: str, b: str) -> bool:
    import hmac as _hmac
    return _hmac.compare_digest(a, b)


def decrypt_event(encrypted: str) -> dict | None:
    """Decrypt a Lark `encrypt` payload (AES-256-CBC, IV prepended). Returns dict."""
    _, _, _, encrypt_key = get_credentials()
    if not encrypt_key:
        return None
    try:
        cipher_bytes = base64.b64decode(encrypted)
        key = hashlib.sha256(encrypt_key.encode()).digest()  # 32-byte AES-256 key
        iv, ct = cipher_bytes[:16], cipher_bytes[16:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ct) + decryptor.finalize()
        pad = padded[-1]
        plaintext = padded[:-pad]  # strip PKCS#7
        return json.loads(plaintext.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.error("event decryption failed: %s", e)
        return None


# ----------------------------- tenant token ---------------------------------

def get_tenant_token() -> str:
    """Return a cached tenant_access_token (refreshed 5 min early)."""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    app_id, app_secret, _, _ = get_credentials()
    if not (app_id and app_secret):
        return ""

    url = f"{_API_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        log.error("tenant token fetch failed: %s", e)
        return ""

    if result.get("code") != 0:
        log.error("tenant token error: %s", result)
        return ""
    _token_cache["token"] = result["tenant_access_token"]
    _token_cache["expires_at"] = time.time() + result.get("expire", 7200)
    return _token_cache["token"]


# ----------------------------- messaging ------------------------------------

_MAX_TEXT_LEN = 20000


def send_message(receive_id: str, text: str, receive_id_type: str = "chat_id") -> bool:
    """Send a text message, chunking to stay under Lark's length limit."""
    token = get_tenant_token()
    if not token:
        log.error("cannot send: no tenant token")
        return False

    url = f"{_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    chunks = [text[i:i + _MAX_TEXT_LEN] for i in range(0, len(text), _MAX_TEXT_LEN)] or [""]
    ok = True
    for chunk in chunks:
        body = json.dumps({
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),  # content is a JSON string
        }).encode()
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            if result.get("code") != 0:
                log.error("send_message error: %s", result)
                ok = False
        except Exception as e:  # noqa: BLE001
            log.error("send_message failed: %s", e)
            ok = False
    return ok


def download_image(image_key: str) -> tuple[bytes | None, str | None]:
    """Download an image resource by key. Returns (bytes, content_type) or (None, None)."""
    token = get_tenant_token()
    if not token:
        return (None, None)
    url = f"{_API_DOMAIN}/open-apis/im/v1/images/{image_key}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            data = resp.read(_MAX_IMAGE_BYTES + 1)
    except Exception as e:  # noqa: BLE001
        log.error("image download failed: %s", e)
        return (None, None)
    if len(data) > _MAX_IMAGE_BYTES or content_type not in _ALLOWED_IMAGE_TYPES:
        log.warning("image rejected (size/type): %s %d", content_type, len(data))
        return (None, None)
    return (data, content_type)
