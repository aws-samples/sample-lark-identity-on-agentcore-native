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


def _call(method: str, url: str, body: dict, bearer: str = "") -> dict:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def _post(url: str, body: dict, bearer: str = "") -> dict:
    return _call("POST", url, body, bearer)


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


def _rid_type(receive_id: str) -> str:
    """`ou_` means an open_id — a DM to that person rather than a chat. Event-driven
    turns (an approval task landing) have no chat to reply in, only the person."""
    return "open_id" if receive_id.startswith("ou_") else "chat_id"


def send_text(chat_id: str, text: str) -> bool:
    """Post text to a chat, splitting to stay under Lark's length limit."""
    tok = _tenant_token()
    if not (tok and chat_id):
        return False
    url = f"{_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type={_rid_type(chat_id)}"
    ok = True
    for i in range(0, len(text) or 1, _MAX_TEXT_LEN):
        chunk = text[i:i + _MAX_TEXT_LEN]
        body = _post(url, {"receive_id": chat_id, "msg_type": "text",
                           "content": json.dumps({"text": chunk})}, bearer=tok)
        if body.get("code") not in (0, "0"):
            log.error("send to %s failed: %s", chat_id, body.get("msg", body))
            ok = False
    return ok


def send_link(chat_id: str, text: str, link_text: str, url: str) -> bool:
    """Post text + a clickable hyperlink, so the user sees "点击授权" rather than a
    raw consent URL — mirrors the router's send_link_message. Used from the async
    turn when a tool hits an authorization wall, so the prompt matches what the
    router shows on the synchronous path."""
    tok = _tenant_token()
    if not (tok and chat_id):
        return False
    post = {"zh_cn": {"content": [[
        {"tag": "text", "text": (text + " ") if text else ""},
        {"tag": "a", "text": link_text, "href": url},
    ]]}}
    body = _post(f"{_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type={_rid_type(chat_id)}",
                 {"receive_id": chat_id, "msg_type": "post",
                  "content": json.dumps(post)}, bearer=tok)
    if body.get("code") not in (0, "0"):
        log.error("send_link to %s failed: %s", chat_id, body.get("msg", body))
        return False
    return True


def add_reaction(message_id: str, emoji: str = "OnIt") -> str:
    """React to the user's message so they know it was received. Returns the
    reaction_id needed to remove it, or "" on failure.

    Faster than any message: one call, no card entity to create. Uses the tenant
    token (the bot reacting as itself) and needs only `im:message`, which the bot
    already holds. A bot may react to a message it didn't send, but may only delete
    reactions it added itself."""
    tok = _tenant_token()
    if not (tok and message_id):
        return ""
    body = _post(f"{_API_DOMAIN}/open-apis/im/v1/messages/{message_id}/reactions",
                 {"reaction_type": {"emoji_type": emoji}}, bearer=tok)
    if body.get("code") not in (0, "0"):
        log.warning("add reaction failed: %s", body.get("msg", body))
        return ""
    return body.get("data", {}).get("reaction_id", "")


def remove_reaction(message_id: str, reaction_id: str) -> bool:
    """Drop the in-progress marker once the turn is done. Best-effort: if the
    container dies mid-turn nobody removes it, which is cosmetic only."""
    if not (message_id and reaction_id):
        return False
    tok = _tenant_token()
    if not tok:
        return False
    body = _call("DELETE",
                 f"{_API_DOMAIN}/open-apis/im/v1/messages/{message_id}"
                 f"/reactions/{reaction_id}", {}, bearer=tok)
    if body.get("code") not in (0, "0"):
        log.warning("remove reaction failed: %s", body.get("msg", body))
        return False
    return True


# --------------------------- CardKit streaming ------------------------------
# A turn is silent for several seconds before the first token (session assembly +
# MCP handshake + model latency, measured ~7.5 s), which reads as "did my message
# even send?". A streaming card fills that gap: post a placeholder at once, then
# type the answer into it. CardKit's streaming mode is the supported mechanism —
# its updates don't count against the QPS limit, unlike editing a message.

_ELEMENT_ID = "answer"
_STREAM_PLACEHOLDER = "🤔 正在思考…"


def _card_json(text: str, streaming: bool) -> dict:
    return {
        "schema": "2.0",
        "config": {"streaming_mode": streaming, "update_multi": True},
        "body": {"elements": [
            {"tag": "markdown", "element_id": _ELEMENT_ID, "content": text},
        ]},
    }


class StreamingCard:
    """One streaming card's lifecycle. All CardKit writes on a card share a single
    strictly-increasing sequence, so it lives here rather than in the caller.

    Every method returns a bool and never raises: streaming is an enhancement, and
    a failure must fall back to send_text rather than lose the answer. `ok` tracks
    whether the card is usable; once a call fails the caller should stop and fall
    back."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.card_id = ""
        self._seq = 0
        self.ok = False
        # Sending happens on its own thread. Measured, one CardKit write takes ~470 ms
        # (361–606 ms), so writing from the token loop stalled it for longer than the
        # flush interval it was tuned against — the loop spent over half its time
        # waiting on HTTP instead of reading the model, and the text arrived in visible
        # jerks. Now the loop only ever hands over the latest accumulated text.
        self._lock = threading.Lock()      # guards _pending
        self._send_lock = threading.Lock()  # one write in flight, so _seq stays ordered
        self._pending: str | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _pump(self) -> None:
        """Write the newest text, repeatedly. Superseded states are dropped rather than
        queued: CardKit takes the full accumulated text on every write, so the newest
        one subsumes every earlier one — queueing them would only add latency."""
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            with self._lock:
                text, self._pending = self._pending, None
            if text is not None:
                self._write(text)

    def _write(self, full_text: str) -> bool:
        """The blocking CardKit write. Serialized, so `sequence` stays ordered."""
        with self._send_lock:
            tok = _tenant_token()
            if not tok:
                return False
            body = _call("PUT",
                         f"{_API_DOMAIN}/open-apis/cardkit/v1/cards/{self.card_id}"
                         f"/elements/{_ELEMENT_ID}/content",
                         {"content": full_text or _STREAM_PLACEHOLDER,
                          "sequence": self._next_seq()}, bearer=tok)
            if body.get("code") not in (0, "0"):
                log.warning("card update failed: %s", body.get("msg", body))
                self.ok = False
                return False
            return True

    def open(self) -> bool:
        """Create the card entity and post it. False if CardKit is unavailable
        (e.g. the cardkit:card:write scope was never granted) — caller falls back."""
        tok = _tenant_token()
        if not (tok and self.chat_id):
            return False
        created = _post(f"{_API_DOMAIN}/open-apis/cardkit/v1/cards",
                        {"type": "card_json",
                         "data": json.dumps(_card_json(_STREAM_PLACEHOLDER, True))},
                        bearer=tok)
        if created.get("code") not in (0, "0"):
            log.warning("card create failed (falling back to text): %s",
                        created.get("msg", created))
            return False
        self.card_id = created.get("data", {}).get("card_id", "")
        if not self.card_id:
            return False
        sent = _post(f"{_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type={_rid_type(self.chat_id)}",
                     {"receive_id": self.chat_id, "msg_type": "interactive",
                      "content": json.dumps({"type": "card",
                                             "data": {"card_id": self.card_id}})},
                     bearer=tok)
        if sent.get("code") not in (0, "0"):
            log.warning("card send failed (falling back to text): %s",
                        sent.get("msg", sent))
            return False
        self.ok = True
        self._worker = threading.Thread(target=self._pump, daemon=True,
                                        name="cardkit-stream")
        self._worker.start()
        return True

    def update(self, full_text: str) -> bool:
        """Hand over the accumulated text and return at once — the write happens on the
        worker. Must be the full text so far, not a delta: CardKit renders the appended
        tail with a typewriter effect.

        The bool reports whether the card is still usable, which is now known one call
        late — a write that fails is discovered by the worker, so this returns False from
        the *next* call onwards. That is what the caller needs it for (stop streaming,
        fall back to text), and close() re-checks before finishing."""
        if not self.ok:
            return False
        with self._lock:
            self._pending = full_text
        self._wake.set()
        return True

    def close(self, final_text: str) -> bool:
        """Write the final text and turn streaming off, so the card stops showing the
        typewriter indicator and becomes static."""
        if not self.ok:
            return False
        # Stop the worker first, then write the final text synchronously: the worker
        # drops superseded states, and the last one must not be dropped.
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=10)
        self._write(final_text)
        if not self.ok:
            return False
        tok = _tenant_token()
        body = _call("PATCH",
                     f"{_API_DOMAIN}/open-apis/cardkit/v1/cards/{self.card_id}/settings",
                     {"settings": json.dumps({"config": {"streaming_mode": False}}),
                      "sequence": self._next_seq()}, bearer=tok)
        if body.get("code") not in (0, "0"):
            log.warning("card close failed: %s", body.get("msg", body))
            return False
        return True
