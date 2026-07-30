"""Send a message back to a Lark chat from inside the agent.

Long turns can't be answered synchronously: the caller's request/response window
is bounded (15 min for InvokeAgentRuntime, less for a Lambda), while a real task —
research something, write several documents — may take longer. So the agent
accepts the work, returns immediately, and pushes the result here when it's done.

This uses the app's own tenant token (not the user's), because it is the bot
speaking to the user. The per-user Lark token is only ever used for reading and
writing that user's own data, via lark_3lo.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

import boto3

log = logging.getLogger("agent.notify")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")
_SECRET_ID = os.environ.get("LARK_SECRET_ID", "")
_MAX_TEXT_LEN = 4000  # Lark rejects longer bodies; split rather than truncate

_secrets = boto3.client("secretsmanager", region_name=_REGION)
_token: tuple[str, float] | None = None  # (token, expires_at)
_lock = threading.Lock()


def _post(url: str, body: dict, bearer: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def _tenant_token() -> str:
    """Cached tenant token. Lark issues these for ~2h; refresh a little early."""
    global _token
    with _lock:
        if _token and _token[1] > time.time():
            return _token[0]
        if not _SECRET_ID:
            log.warning("LARK_SECRET_ID unset — cannot notify the chat")
            return ""
        secret = json.loads(_secrets.get_secret_value(SecretId=_SECRET_ID)["SecretString"])
        body = _post(f"{_API_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
                     {"app_id": secret["appId"], "app_secret": secret["appSecret"]})
        tok = body.get("tenant_access_token", "")
        if not tok:
            log.error("tenant token request failed: %s", body.get("msg", body))
            return ""
        _token = (tok, time.time() + int(body.get("expire", 7200)) - 300)
        return tok


def send_text(chat_id: str, text: str) -> bool:
    """Post text to a chat, splitting to stay under Lark's length limit."""
    tok = _tenant_token()
    if not (tok and chat_id):
        return False
    url = f"{_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id"
    ok = True
    for i in range(0, len(text) or 1, _MAX_TEXT_LEN):
        chunk = text[i:i + _MAX_TEXT_LEN]
        body = _post(url, {"receive_id": chat_id, "msg_type": "text",
                           "content": json.dumps({"text": chunk})}, bearer=tok)
        if body.get("code") not in (0, "0"):
            log.error("send to %s failed: %s", chat_id, body.get("msg", body))
            ok = False
    return ok
