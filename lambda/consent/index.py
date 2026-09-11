"""Lark 3LO return endpoint — the one piece of the consent flow that stays ours.

The credential provider registers Lark's own authorize/token endpoints directly, so no
translation façade sits in the OAuth path any more (v2 accepts form-encoded requests and
returns standard errors; the newer v3 endpoint is RFC 6749 but PKCE is not wired into it
yet — see .dev/LARK_OAUTH_SPEC.md). What AgentCore still needs from
us is a return-url: after consent it 302s the browser here with a session id, and the
consent must be completed (CompleteResourceTokenAuth) and the parked turn resumed.

Routes (API Gateway HTTP API, payload v2):
  GET  /return  -> delegate completion to the router (it mints the user's JWT),
                   poke the consent-resume replay, DM the user, show a done page

History: this Lambda used to also host /authorize and /token as an RFC-6749 façade
over Lark's deprecated open.*/authen/v2 token endpoint (JSON-only body, occasional
200-on-error). Lark v3 made that translation unnecessary.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error      # not implied by urllib.request — only bound if imported
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_REGION", "us-west-2")
# Lark API host for the consent-complete DM (tenant token + im/v1/messages).
_TOKEN_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)
_secrets = boto3.client("secretsmanager", region_name=_REGION)
# Set to notify the user in chat once consent completes (see _notify_chat).
_LARK_SECRET_ID = os.environ.get("LARK_SECRET_ID", "")
# The router function to poke after consent, so it can replay the parked message.
# Empty → no resume (consent still completes; the user just re-sends). Passed in
# rather than hardcoded so the name stays owned by the router stack.
_ROUTER_FUNCTION = os.environ.get("ROUTER_FUNCTION_NAME", "")
_lambda = boto3.client("lambda", region_name=_REGION)


# ------------------------------- helpers ------------------------------------

def _resp(status: int, body: dict, headers: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return {"statusCode": status, "headers": h, "body": json.dumps(body)}


def _b64url_decode(s: str) -> str:
    """Decode a base64url userId from `state`; if it isn't valid b64url, return as-is."""
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad).decode()
    except Exception:  # noqa: BLE001
        return s


def handle_return(qs: dict) -> dict:
    """3LO return-url: bind the completed consent to the initiating session.

    AgentCore Identity redirects the browser here with a session id after the
    user consents on Lark; CompleteResourceTokenAuth verifies initiator==completer
    and lets Identity vault the token. Then show a static done page.
    """
    logger.info("return qs: %s", json.dumps(qs))  # see exactly what AgentCore appends
    session_id = qs.get("session_id") or qs.get("sessionId", "")
    # userId is carried via customState (echoed back as `state`), base64url-encoded
    # because AgentCore rejects ':' in state (it corrupts the requestUri validation).
    # The return URL itself stays bare (workload allowlist is exact-match).
    raw_state = qs.get("state") or qs.get("uid", "")
    user_id = _b64url_decode(raw_state) if raw_state else ""
    if not session_id:
        return _resp(400, {"error": "missing session_id", "received": qs})
    if not user_id:
        return _resp(400, {"error": "missing state/uid (userId)", "received": qs})
    # Completion is delegated to the router, synchronously: the consent session lives in
    # the JWT-derived vault namespace, so it can only be completed by naming the user
    # with their signed token — and the router is the one component that mints those.
    # Keeping the minting there also keeps the password salt out of this façade.
    err = _complete_via_router(user_id, session_id)
    if err:
        return {"statusCode": 502, "headers": {"Content-Type": "text/html"},
                "body": f"<h3>Authorization failed</h3><p>{err}</p>"}
    # Tell the user in chat — the browser only sees this static page, and the
    # router can't reliably detect completion by polling (a re-auth keeps the old
    # token until the new one lands). This is the precise signal.
    # Callback-driven resume: tell the router the user has consented, so it can
    # replay whatever message hit the auth wall. Best-effort and asynchronous — if
    # it fails or nothing was parked, the user simply sends again. Done before the
    # notify so a resumed answer, not a "you can ask now", is what they see.
    _resume_router(user_id)
    _notify_chat(user_id, "✅ 授权成功，正在继续处理…")
    return {"statusCode": 200, "headers": {"Content-Type": "text/html"},
            "body": "<h3>Authorized</h3><p>You can close this tab and return to the chat.</p>"}


def _post_json(url: str, body: dict, bearer: str = "") -> dict:
    """POST JSON, returning the parsed body (errors included — caller decides)."""
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


def _complete_via_router(actor_id: str, session_uri: str) -> str:
    """Ask the router to complete this consent. Returns "" on success, else the error
    to show the user. Synchronous — the browser is waiting on the answer."""
    if not _ROUTER_FUNCTION:
        return "router function not configured (ROUTER_FUNCTION_NAME)"
    try:
        resp = _lambda.invoke(
            FunctionName=_ROUTER_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps({"_complete_consent": True, "actorId": actor_id,
                                "sessionUri": session_uri}).encode(),
        )
        out = json.loads(resp["Payload"].read().decode() or "{}")
    except Exception as e:  # noqa: BLE001
        logger.exception("router consent completion invoke failed")
        return f"{type(e).__name__}: {e}"
    if out.get("ok"):
        return ""
    return str(out.get("error") or out.get("errorMessage") or "unknown error")


def _resume_router(actor_id: str) -> None:
    """Fire-and-forget invoke of the router's consent-resume path."""
    if not _ROUTER_FUNCTION:
        return
    try:
        _lambda.invoke(
            FunctionName=_ROUTER_FUNCTION,
            InvocationType="Event",  # async — we don't wait for the replay
            Payload=json.dumps({"_consent_resumed": True, "actorId": actor_id}).encode(),
        )
    except Exception:  # noqa: BLE001 — resume is a nicety; consent already succeeded
        logger.exception("failed to trigger router resume for %s", actor_id)


def _notify_chat(actor_id: str, text: str) -> None:
    """Best-effort DM to the user who just consented. actor_id is "lark:{open_id}";
    Lark takes open_id directly as receive_id, so no chat_id lookup is needed."""
    if not _LARK_SECRET_ID:
        return
    open_id = actor_id.split(":", 1)[1] if ":" in actor_id else actor_id
    try:
        secret = json.loads(
            _secrets.get_secret_value(SecretId=_LARK_SECRET_ID)["SecretString"])
        tok = _post_json(
            f"{_TOKEN_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": secret["appId"], "app_secret": secret["appSecret"]},
        ).get("tenant_access_token", "")
        if not tok:
            logger.warning("notify: no tenant token")
            return
        _post_json(
            f"{_TOKEN_DOMAIN}/open-apis/im/v1/messages?receive_id_type=open_id",
            {"receive_id": open_id, "msg_type": "text",
             "content": json.dumps({"text": text})},
            bearer=tok,
        )
        logger.info("notify: told %s the consent completed", actor_id)
    except Exception:
        logger.exception("notify: failed to message %s", actor_id)


# ------------------------------- entry --------------------------------------

def handler(event: dict, _context) -> dict:
    path = event.get("rawPath", event.get("requestContext", {}).get("http", {}).get("path", ""))
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    qs = event.get("queryStringParameters") or {}
    headers = event.get("headers", {})
    logger.info("consent hit: method=%s path=%s", method, path)

    if path.endswith("/return") and method == "GET":
        return handle_return(qs)
    return _resp(404, {"error": "not_found", "error_description": path})
