"""Mint a Cognito JWT for a Lark-authenticated user (web_api side).

Mirrors the container's identity module: username = ``lark:{open_id}``,
HMAC-derived deterministic password, auto-provision on first login. This is
what turns "Lark is the IdP" into a standard OIDC JWT that the API Gateway /
AgentCore JWT authorizers can validate.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import logging

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("web_api.cognito")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
_PASSWORD_SECRET_ID = os.environ["COGNITO_PASSWORD_SECRET_ID"]

_cognito = boto3.client("cognito-idp", region_name=_REGION)
_secrets = boto3.client("secretsmanager", region_name=_REGION)

_salt: str | None = None


def _get_salt() -> str:
    global _salt
    if _salt is None:
        _salt = _secrets.get_secret_value(SecretId=_PASSWORD_SECRET_ID)["SecretString"]
    return _salt


def _password(username: str) -> str:
    digest = hmac.new(_get_salt().encode(), username.encode(), hashlib.sha256).hexdigest()
    return digest[:32] + "Aa1!"


def _ensure_user(username: str, email: str) -> None:
    try:
        _cognito.admin_get_user(UserPoolId=_USER_POOL_ID, Username=username)
    except ClientError as e:
        if e.response["Error"]["Code"] != "UserNotFoundException":
            raise
        _cognito.admin_create_user(
            UserPoolId=_USER_POOL_ID, Username=username,
            UserAttributes=[{"Name": "email", "Value": email or f"{username.replace(':', '-')}@lark.local"},
                            {"Name": "email_verified", "Value": "true"}],
            MessageAction="SUPPRESS",
        )
    _cognito.admin_set_user_password(
        UserPoolId=_USER_POOL_ID, Username=username,
        Password=_password(username), Permanent=True,
    )


def mint_id_token(open_id: str, email: str = "") -> tuple[str, str]:
    """Return (id_token, actor_id) for a Lark open_id. actor_id == username."""
    username = f"lark:{open_id}"
    _ensure_user(username, email)
    resp = _cognito.admin_initiate_auth(
        UserPoolId=_USER_POOL_ID, ClientId=_CLIENT_ID,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": _password(username)},
    )
    return resp["AuthenticationResult"]["IdToken"], username
