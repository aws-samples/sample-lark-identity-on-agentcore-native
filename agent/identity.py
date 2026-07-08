"""Per-user identity → Cognito JWT minting inside the agent container.

The agent never holds downstream tool credentials. Instead, for each end-user it
mints a short-lived Cognito JWT (username = ``lark:{open_id}``) and attaches it as
a Bearer token on outbound MCP/Gateway calls. The Gateway's JWT authorizer
verifies it and the Request Interceptor derives the user's tenant from its claims.

Passwords are HMAC-derived from a Secrets Manager salt, so they are deterministic
and never stored. Users are auto-provisioned on first use via AdminCreateUser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import logging

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("agent.identity")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
_PASSWORD_SECRET_ID = os.environ.get("COGNITO_PASSWORD_SECRET_ID", "")

_cognito = boto3.client("cognito-idp", region_name=_REGION)
_secrets = boto3.client("secretsmanager", region_name=_REGION)

# module-level caches (survive across warm invocations)
_password_salt: str | None = None
_token_cache: dict[str, tuple[str, float]] = {}  # username -> (id_token, exp_epoch)


def _get_salt() -> str:
    global _password_salt
    if _password_salt is None:
        resp = _secrets.get_secret_value(SecretId=_PASSWORD_SECRET_ID)
        _password_salt = resp["SecretString"]
    return _password_salt


def _derive_password(username: str) -> str:
    """Deterministic password: HMAC-SHA256(salt, username), 32-char + fixed suffix.

    Suffix guarantees Cognito complexity even though the pool policy is relaxed.
    """
    digest = hmac.new(_get_salt().encode(), username.encode(), hashlib.sha256).hexdigest()
    return digest[:32] + "Aa1!"


def _ensure_user(username: str, email: str = "") -> None:
    try:
        _cognito.admin_get_user(UserPoolId=_USER_POOL_ID, Username=username)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise
    # username is "lark:ou_xxx" — colon is invalid in an email local part
    safe_local = username.replace(":", "-")
    attrs = [{"Name": "email", "Value": email or f"{safe_local}@lark.local"},
             {"Name": "email_verified", "Value": "true"}]
    _cognito.admin_create_user(
        UserPoolId=_USER_POOL_ID,
        Username=username,
        UserAttributes=attrs,
        MessageAction="SUPPRESS",
    )
    _cognito.admin_set_user_password(
        UserPoolId=_USER_POOL_ID,
        Username=username,
        Password=_derive_password(username),
        Permanent=True,
    )
    log.info("provisioned cognito user %s", username)


def _jwt_exp(id_token: str) -> float:
    """Read the ``exp`` claim without verifying (Cognito already signed it)."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0.0


def get_user_jwt(username: str, email: str = "") -> str:
    """Return a valid Cognito ID token for ``username`` (e.g. ``lark:ou_xxx``).

    Cached with a 60s early-refresh margin. Provisions the user on first use.
    """
    if not (_USER_POOL_ID and _CLIENT_ID and _PASSWORD_SECRET_ID):
        raise RuntimeError("Cognito env not configured (COGNITO_USER_POOL_ID/CLIENT_ID/PASSWORD_SECRET_ID)")

    cached = _token_cache.get(username)
    if cached and time.time() < cached[1] - 60:
        return cached[0]

    _ensure_user(username, email)
    try:
        resp = _cognito.admin_initiate_auth(
            UserPoolId=_USER_POOL_ID,
            ClientId=_CLIENT_ID,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": _derive_password(username)},
        )
    except ClientError as e:
        # password drift (salt rotated) — reset and retry once
        if e.response["Error"]["Code"] in ("NotAuthorizedException", "UserNotFoundException"):
            _ensure_user(username, email)
            _cognito.admin_set_user_password(
                UserPoolId=_USER_POOL_ID, Username=username,
                Password=_derive_password(username), Permanent=True,
            )
            resp = _cognito.admin_initiate_auth(
                UserPoolId=_USER_POOL_ID, ClientId=_CLIENT_ID,
                AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": _derive_password(username)},
            )
        else:
            raise

    # Use the ACCESS token: the Gateway's allowedClients check validates the
    # `client_id` claim, which only access tokens carry (ID tokens have `aud`
    # and would fail with insufficient_scope). The interceptor reads identity
    # from the access token's `username` claim.
    token = resp["AuthenticationResult"]["AccessToken"]
    _token_cache[username] = (token, _jwt_exp(token))
    return token
