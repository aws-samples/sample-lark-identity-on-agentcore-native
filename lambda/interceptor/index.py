"""Gateway Request Interceptor — inject a per-user downstream credential.

The AgentCore Gateway invokes this Lambda before forwarding a tool request to
its target (with passRequestHeaders=true, so we see the caller's JWT). We:
  1. Read the end-user identity from the (already Gateway-verified) JWT.
  2. Map the user to a tenant and fetch that tenant's downstream API key from
     Secrets Manager.
  3. Inject the key as an Authorization header AND surface the resolved identity
     to the target as X-End-User-* headers.

The agent never holds the downstream key; the user's identity travels to the
tool. See docs/superpowers/specs/2026-05-12-gateway-interceptor-credential-injection.md.

Interceptor I/O contract:
  event.mcp.gatewayRequest.{headers,body}  ->  {interceptorOutputVersion, mcp:{transformedGatewayRequest:{headers,body}}}
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_TOOL_KEYS_PREFIX = os.environ.get("TOOL_KEYS_SECRET_PREFIX", "lark-agent/tool-keys")

_secrets = boto3.client("secretsmanager", region_name=_REGION)
_key_cache: dict[str, tuple[str, float]] = {}  # tenant -> (key, exp)


def _decode_jwt_claims(auth_header: str) -> dict:
    """Decode JWT payload WITHOUT verifying — the Gateway authorizer already did."""
    if not auth_header.startswith("Bearer "):
        return {}
    token = auth_header[len("Bearer "):]
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _identity(claims: dict) -> tuple[str, str]:
    """Return (actor_id, tenant). actor_id == 'lark:{open_id}'."""
    username = claims.get("cognito:username") or claims.get("username") or "anonymous"
    # Single-tenant PoC: everyone maps to 'default'. A real deployment would map
    # e.g. the Lark tenant_key claim or an email domain here.
    tenant = claims.get("custom:tenant") or "default"
    return username, tenant


def _get_key(tenant: str) -> str:
    cached = _key_cache.get(tenant)
    if cached and time.time() < cached[1]:
        return cached[0]
    secret_id = f"{_TOOL_KEYS_PREFIX}/{tenant}"
    try:
        raw = _secrets.get_secret_value(SecretId=secret_id)["SecretString"]
        key = json.loads(raw).get("api_key", "")
    except Exception as e:  # noqa: BLE001
        logger.warning("no key for tenant %s (%s); using empty", tenant, e)
        key = ""
    _key_cache[tenant] = (key, time.time() + 300)
    return key


def handler(event, context):
    mcp = event.get("mcp", {})
    gw_req = mcp.get("gatewayRequest", {})
    headers = dict(gw_req.get("headers", {}))
    body = gw_req.get("body", {})

    claims = _decode_jwt_claims(headers.get("Authorization", headers.get("authorization", "")))
    actor_id, tenant = _identity(claims)
    key = _get_key(tenant)

    out_headers = {
        "Content-Type": "application/json",
        # downstream credential injected by the gateway, not held by the agent
        "Authorization": f"Bearer {key}" if key else "",
    }

    # Lambda targets only receive the Authorization header (via clientContext
    # propagated headers) — custom headers are dropped. So forward the verified
    # end-user identity in the tools/call arguments (the Lambda target's event).
    if isinstance(body, dict) and body.get("method") == "tools/call":
        args = body.setdefault("params", {}).setdefault("arguments", {})
        args["_endUserId"] = actor_id
        args["_endUserTenant"] = tenant

    logger.info("interceptor: actor=%s tenant=%s key=%s", actor_id, tenant, "set" if key else "none")

    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": out_headers,
                "body": body,
            }
        },
    }
