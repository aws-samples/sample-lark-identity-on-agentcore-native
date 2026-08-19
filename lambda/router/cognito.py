"""Per-user Cognito JWT minting — the router's "token factory".

The router is the only component that turns a Lark `open_id` into a signed identity.
Everything downstream then verifies a signature instead of trusting a string: the
Runtime's CUSTOM_JWT authorizer validates the token on the way in, and AgentCore
Identity keys the Token Vault off the token's `sub`.

Access tokens, not id tokens: `client_id` (which authorizers match on) is only in the
access token, and it is the access token AgentCore derives the vault identity from.

Passwords are HMAC-derived from a Secrets Manager salt, so they are deterministic and
never stored. Same scheme as agent/identity.py — that copy stays, it mints the token
the web-search Gateway authorises with.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("router.cognito")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
_PASSWORD_SECRET_ID = os.environ.get("COGNITO_PASSWORD_SECRET_ID", "")

_cognito = boto3.client("cognito-idp", region_name=_REGION)
_secrets = boto3.client("secretsmanager", region_name=_REGION)

_salt: str | None = None
_cache: dict[str, tuple[str, float]] = {}   # username -> (access_token, exp_epoch)


def configured() -> bool:
    return bool(_USER_POOL_ID and _CLIENT_ID and _PASSWORD_SECRET_ID)


def _get_salt() -> str:
    global _salt
    if _salt is None:
        _salt = _secrets.get_secret_value(SecretId=_PASSWORD_SECRET_ID)["SecretString"]
    return _salt


def _password(username: str) -> str:
    digest = hmac.new(_get_salt().encode(), username.encode(), hashlib.sha256).hexdigest()
    return digest[:32] + "Aa1!"   # suffix satisfies Cognito complexity


def _ensure_user(username: str) -> None:
    try:
        _cognito.admin_get_user(UserPoolId=_USER_POOL_ID, Username=username)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise
    _cognito.admin_create_user(
        UserPoolId=_USER_POOL_ID, Username=username, MessageAction="SUPPRESS",
        # A colon is invalid in an email local part, and the username carries one.
        UserAttributes=[{"Name": "email", "Value": f"{username.replace(':', '-')}@lark.local"},
                        {"Name": "email_verified", "Value": "true"}],
    )
    _cognito.admin_set_user_password(
        UserPoolId=_USER_POOL_ID, Username=username,
        Password=_password(username), Permanent=True,
    )
    log.info("provisioned cognito user %s", username)


def _exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


def user_jwt(actor_id: str) -> str:
    """A valid Cognito access token for `actor_id` ("lark:{open_id}").

    Cached with a 60 s early-refresh margin; provisions the user on first use.
    """
    if not configured():
        raise RuntimeError("Cognito env not configured "
                           "(COGNITO_USER_POOL_ID / CLIENT_ID / PASSWORD_SECRET_ID)")
    hit = _cache.get(actor_id)
    if hit and time.time() < hit[1] - 60:
        return hit[0]
    _ensure_user(actor_id)
    params = {"USERNAME": actor_id, "PASSWORD": _password(actor_id)}
    try:
        resp = _cognito.admin_initiate_auth(
            UserPoolId=_USER_POOL_ID, ClientId=_CLIENT_ID,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters=params)
    except ClientError as e:
        # Salt rotated → the derived password no longer matches. Reset and retry once.
        if e.response["Error"]["Code"] not in ("NotAuthorizedException", "UserNotFoundException"):
            raise
        _ensure_user(actor_id)
        _cognito.admin_set_user_password(
            UserPoolId=_USER_POOL_ID, Username=actor_id,
            Password=_password(actor_id), Permanent=True)
        resp = _cognito.admin_initiate_auth(
            UserPoolId=_USER_POOL_ID, ClientId=_CLIENT_ID,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH", AuthParameters=params)
    token = resp["AuthenticationResult"]["AccessToken"]
    _cache[actor_id] = (token, _exp(token))
    return token
