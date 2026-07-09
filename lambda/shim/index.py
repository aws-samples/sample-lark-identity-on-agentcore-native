"""Lark OAuth shim — an RFC-6749 façade in front of Lark's non-standard token endpoint.

AgentCore Identity's custom OAuth2 provider speaks standard OAuth2 (form-encoded
token requests, JSON responses, non-2xx on error). Lark's authen/v2/oauth/token
uses a JSON request body, a `code:"0"`-wrapped response, and sometimes HTTP-200
on error. This shim translates between the two so Lark can be registered as a
provider. See .dev/LARK_OAUTH_SPEC.md for the exact Lark shapes.

Routes (API Gateway HTTP API, payload v2):
  GET  /authorize  -> 302 to Lark's accounts.* authorize URL (pass-through params)
  POST /token      -> form-encoded RFC request -> Lark JSON call -> clean JSON / 4xx
  GET  /return     -> 3LO return-url: calls CompleteResourceTokenAuth, shows a done page

The authorize host (accounts.larksuite.com) differs from the token host
(open.larksuite.com) — token domain comes from LARK_API_DOMAIN, authorize domain
is derived from it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_REGION", "us-west-2")
# Token host, e.g. https://open.larksuite.com (or open.feishu.cn).
_TOKEN_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")
# Authorize lives on the accounts.* host, not open.*.
_AUTHORIZE_DOMAIN = (
    os.environ.get("LARK_ACCOUNTS_DOMAIN")
    or _TOKEN_DOMAIN.replace("open.larksuite.com", "accounts.larksuite.com")
              .replace("open.feishu.cn", "accounts.feishu.cn")
)
_TOKEN_URL = f"{_TOKEN_DOMAIN}/open-apis/authen/v2/oauth/token"
_AUTHORIZE_URL = f"{_AUTHORIZE_DOMAIN}/open-apis/authen/v1/authorize"

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)


# ------------------------------- helpers ------------------------------------

def _resp(status: int, body: dict, headers: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    return {"statusCode": status, "headers": h, "body": json.dumps(body)}


def _client_creds(form: dict, headers: dict) -> tuple[str, str]:
    """Client id/secret via client_secret_post (form) or client_secret_basic (header)."""
    cid, secret = form.get("client_id", ""), form.get("client_secret", "")
    if not cid or not secret:
        auth = headers.get("authorization", headers.get("Authorization", ""))
        if auth.lower().startswith("basic "):
            raw = base64.b64decode(auth[6:]).decode()
            cid, _, secret = raw.partition(":")
    return cid, secret


def _post_lark(payload: dict) -> tuple[int, dict]:
    """POST JSON to Lark's token endpoint. Returns (http_status, parsed_body).

    Lark may answer 200-with-nonzero-code OR 4xx; urlopen raises on 4xx/5xx, so
    read the error body back out to keep the parsed shape uniform.
    """
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return e.code, {"code": str(e.code), "error": "server_error",
                            "error_description": "Lark returned a non-JSON error"}


# ------------------------------- routes -------------------------------------

def handle_authorize(qs: dict) -> dict:
    """302 to Lark's authorize URL, passing through the standard OAuth params.

    AgentCore appends client_id/redirect_uri/response_type/scope/state; forward
    exactly what it sent (Lark expects the same names). scope must already carry
    offline_access for a refresh_token to come back.
    """
    passthrough = {k: qs[k] for k in
                   ("client_id", "redirect_uri", "response_type", "scope", "state",
                    "code_challenge", "code_challenge_method")
                   if k in qs and qs[k]}
    passthrough.setdefault("response_type", "code")
    location = f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(passthrough)}"
    logger.info("authorize -> %s", location)
    return {"statusCode": 302, "headers": {"Location": location}, "body": ""}


def handle_token(form: dict, headers: dict) -> dict:
    """Translate a form-encoded RFC token request into Lark's JSON call."""
    grant = form.get("grant_type", "")
    cid, secret = _client_creds(form, headers)
    if not cid or not secret:
        return _resp(400, {"error": "invalid_client",
                           "error_description": "missing client_id/client_secret"})

    if grant == "authorization_code":
        payload = {"grant_type": grant, "client_id": cid, "client_secret": secret,
                   "code": form.get("code", "")}
        if form.get("redirect_uri"):
            payload["redirect_uri"] = form["redirect_uri"]
        # PKCE: AgentCore Identity uses code_challenge on /authorize, so Lark
        # requires the matching code_verifier here. Forward it (and passthrough
        # code_challenge* on the off chance a caller sends them).
        for k in ("code_verifier", "code_challenge", "code_challenge_method"):
            if form.get(k):
                payload[k] = form[k]
    elif grant == "refresh_token":
        payload = {"grant_type": grant, "client_id": cid, "client_secret": secret,
                   "refresh_token": form.get("refresh_token", "")}
        if form.get("scope"):
            payload["scope"] = form["scope"]  # only narrows, per Lark
    else:
        return _resp(400, {"error": "unsupported_grant_type",
                           "error_description": f"grant_type={grant!r}"})

    http_status, body = _post_lark(payload)

    # Success is judged by access_token presence, not code type (Lark's `code`
    # is the string "0" but examples show bare 0 — don't compare it).
    if body.get("access_token"):
        clean = {
            "access_token": body["access_token"],
            "token_type": body.get("token_type", "Bearer"),
            "expires_in": body.get("expires_in"),
            "scope": body.get("scope", ""),
        }
        if body.get("refresh_token"):
            clean["refresh_token"] = body["refresh_token"]
            clean["refresh_token_expires_in"] = body.get("refresh_token_expires_in")
        return _resp(200, clean)

    # Error: force a non-2xx even if Lark answered HTTP 200 with a nonzero code.
    status = http_status if http_status >= 400 else 400
    logger.warning("token error from Lark (lark_code=%s): %s", body.get("code"), body)
    return _resp(status, {
        "error": body.get("error", "invalid_grant"),
        "error_description": body.get("error_description",
                                      f"Lark code {body.get('code')}"),
    })


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
    try:
        # CompleteResourceTokenAuth needs BOTH sessionUri and a userIdentifier struct.
        _agentcore.complete_resource_token_auth(
            sessionUri=session_id,
            userIdentifier={"userId": user_id},
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("CompleteResourceTokenAuth failed")
        return {"statusCode": 502, "headers": {"Content-Type": "text/html"},
                "body": f"<h3>Authorization failed</h3><p>{e}</p>"}
    return {"statusCode": 200, "headers": {"Content-Type": "text/html"},
            "body": "<h3>Authorized</h3><p>You can close this tab and return to the chat.</p>"}


# ------------------------------- entry --------------------------------------

def _parse_form(event: dict) -> dict:
    body = event.get("body", "") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def handler(event: dict, _context) -> dict:
    path = event.get("rawPath", event.get("requestContext", {}).get("http", {}).get("path", ""))
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    qs = event.get("queryStringParameters") or {}
    headers = event.get("headers", {})
    logger.info("shim hit: method=%s path=%s", method, path)

    if path.endswith("/authorize") and method == "GET":
        return handle_authorize(qs)
    if path.endswith("/token") and method == "POST":
        return handle_token(_parse_form(event), headers)
    if path.endswith("/return") and method == "GET":
        return handle_return(qs)
    return _resp(404, {"error": "not_found", "error_description": path})
