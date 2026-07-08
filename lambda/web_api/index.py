"""Web API Lambda — Lark login exchange + session bootstrap for the web UI.

Routes (API Gateway HTTP API v2):
  POST /api/lark/auth   (no authorizer)  code -> Cognito JWT (Lark is the IdP)
  POST /api/session     (JWT authorizer) mint sessionId + presigned WSS URL
  GET  /api/session     (JWT authorizer) refresh presigned WSS URL

Identity correctness: the session routes derive identity from the
`cognito:username` claim (== `lark:{open_id}`), NOT `sub` (a random Cognito
UUID). This is what makes a web-UI user and a webhook user the same person.
"""

from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote, urlencode

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

import identity
import lark_oauth
import cognito_mint

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT")
PRESIGNED_EXPIRES = int(os.environ.get("PRESIGNED_URL_EXPIRES", "300"))

_agentcore = boto3.client(
    "bedrock-agentcore", region_name=AWS_REGION,
    config=Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 0}),
)
_session = boto3.Session()


# ----------------------------- presigned WSS --------------------------------

def generate_presigned_ws_url(session_id: str, expires: int = 300) -> str:
    """SigV4-presigned WSS URL to the AgentCore Runtime for this session.

    The browser connects with no AWS creds; AgentCore bridges the connection to
    the agent's WS port inside the microVM.
    """
    encoded_arn = quote(RUNTIME_ARN, safe="")
    base = f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{encoded_arn}/ws"
    params = {
        "qualifier": QUALIFIER,
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    url = f"{base}?{urlencode(params)}"
    creds = _session.get_credentials().get_frozen_credentials()
    req = AWSRequest(method="GET", url=url,
                     headers={"host": f"bedrock-agentcore.{AWS_REGION}.amazonaws.com"})
    SigV4QueryAuth(creds, "bedrock-agentcore", AWS_REGION, expires=expires).add_auth(req)
    return req.url.replace("https://", "wss://")


def warmup(session_id: str, user_id: str, actor_id: str) -> str:
    try:
        resp = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=RUNTIME_ARN, qualifier=QUALIFIER,
            runtimeSessionId=session_id, runtimeUserId=actor_id,
            payload=json.dumps({"action": "warmup", "userId": user_id,
                                "actorId": actor_id, "channel": "lark"}).encode(),
            contentType="application/json", accept="application/json",
        )
        raw = resp["response"].read().decode() if hasattr(resp["response"], "read") else resp["response"]
        return "ready" if json.loads(raw).get("ready") else "starting"
    except Exception as e:  # noqa: BLE001
        logger.warning("warmup failed (non-fatal): %s", e)
        return "error"


# ----------------------------- http helpers ---------------------------------

def _cors():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }


def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json", **_cors()},
            "body": json.dumps(body)}


def _claims(event: dict) -> dict | None:
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return None


# ----------------------------- route handlers -------------------------------

def handle_lark_auth(event: dict) -> dict:
    """Public: exchange a Lark login code for a Cognito JWT."""
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _resp(400, {"error": "invalid JSON"})
    code = body.get("code")
    if not code:
        return _resp(400, {"error": "code required"})
    try:
        user = lark_oauth.exchange_code(code)
        if not user["open_id"]:
            return _resp(401, {"error": "Lark did not return an open_id"})
        id_token, actor_id = cognito_mint.mint_id_token(user["open_id"], user["email"])
        # provision app-side identity (allowlist-gated) up front
        identity.resolve_user("lark", user["open_id"], user["name"])
        return _resp(200, {"idToken": id_token, "actorId": actor_id,
                           "openId": user["open_id"], "name": user["name"]})
    except Exception as e:  # noqa: BLE001
        logger.exception("lark auth failed")
        return _resp(500, {"error": str(e)})


def _identity_from_claims(claims: dict) -> tuple[str, str]:
    """Return (actor_id, open_id) from the JWT. Uses cognito:username, not sub."""
    username = claims.get("cognito:username") or claims.get("username") or ""
    # username == "lark:{open_id}"
    open_id = username.split(":", 1)[1] if ":" in username else username
    return username, open_id


def handle_create_session(event: dict) -> dict:
    claims = _claims(event)
    if not claims:
        return _resp(401, {"error": "missing JWT"})
    actor_id, open_id = _identity_from_claims(claims)
    if not open_id:
        return _resp(400, {"error": "JWT missing username"})

    user_id, is_new = identity.resolve_user("lark", open_id, claims.get("email", ""))
    if user_id is None:
        return _resp(403, {"error": "not allowed", "actorId": actor_id})

    session_id = identity.get_or_create_session(user_id)
    status = warmup(session_id, user_id, actor_id)
    ws_url = generate_presigned_ws_url(session_id, PRESIGNED_EXPIRES)
    return _resp(200, {"sessionId": session_id, "wsUrl": ws_url,
                       "wsExpires": PRESIGNED_EXPIRES, "status": status,
                       "userId": user_id, "isNew": is_new})


def handle_get_session(event: dict) -> dict:
    claims = _claims(event)
    if not claims:
        return _resp(401, {"error": "missing JWT"})
    actor_id, open_id = _identity_from_claims(claims)
    user_id, _ = identity.resolve_user("lark", open_id, claims.get("email", ""))
    if user_id is None:
        return _resp(403, {"error": "not allowed"})
    session_id = identity.get_or_create_session(user_id)
    ws_url = generate_presigned_ws_url(session_id, PRESIGNED_EXPIRES)
    return _resp(200, {"sessionId": session_id, "wsUrl": ws_url,
                       "wsExpires": PRESIGNED_EXPIRES, "userId": user_id})


# ----------------------------- router ---------------------------------------

def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors(), "body": ""}

    if path.endswith("/api/lark/auth") and method == "POST":
        return handle_lark_auth(event)
    if path.endswith("/api/session") and method == "POST":
        return handle_create_session(event)
    if path.endswith("/api/session") and method == "GET":
        return handle_get_session(event)
    return _resp(404, {"error": "not found"})
