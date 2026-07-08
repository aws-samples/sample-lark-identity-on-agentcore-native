"""User identity resolution + allowlist over the DynamoDB identity table.

Identity is `lark:{open_id}` for every entrypoint (webhook + web UI), so a user's
profile/session is shared. Table layout (single-table):
  CHANNEL#lark:{open_id} / PROFILE   -> {userId}
  USER#{userId}          / PROFILE   -> profile
  USER#{userId}          / SESSION   -> {sessionId, lastActivity}
  ALLOW#lark:{open_id}   / ALLOW     -> allowlist entry
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
import logging

import boto3

log = logging.getLogger("router.identity")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
_REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"

_table = boto3.resource("dynamodb", region_name=_REGION).Table(_TABLE_NAME)


def is_user_allowed(channel: str, channel_user_id: str) -> bool:
    if _REGISTRATION_OPEN:
        return True
    key = f"{channel}:{channel_user_id}"
    resp = _table.get_item(Key={"PK": f"ALLOW#{key}", "SK": "ALLOW"})
    return "Item" in resp


def resolve_user(channel: str, channel_user_id: str, display_name: str = "") -> tuple[str | None, bool]:
    """Return (internal_user_id, is_new). None if not allowed and new."""
    channel_key = f"{channel}:{channel_user_id}"
    resp = _table.get_item(Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"})
    if "Item" in resp:
        return resp["Item"]["userId"], False

    # New user — gate on allowlist
    if not is_user_allowed(channel, channel_user_id):
        return None, False

    user_id = "user_" + hashlib.sha256(channel_key.encode()).hexdigest()[:16]
    now = int(time.time())
    _table.put_item(Item={
        "PK": f"USER#{user_id}", "SK": "PROFILE",
        "displayName": display_name, "createdAt": now,
    })
    _table.put_item(Item={
        "PK": f"CHANNEL#{channel_key}", "SK": "PROFILE",
        "userId": user_id, "channel": channel,
    })
    _table.put_item(Item={
        "PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
        "channel": channel,
    })
    return user_id, True


def get_or_create_session(user_id: str) -> str:
    """Deterministic-ish session id per user; refreshed lastActivity on reuse."""
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"})
    if "Item" in resp:
        _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": "SESSION"},
            UpdateExpression="SET lastActivity = :t",
            ExpressionAttributeValues={":t": int(time.time())},
        )
        return resp["Item"]["sessionId"]

    # AgentCore requires the session id to be >= 33 chars.
    session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
    if len(session_id) < 33:
        session_id = session_id + "0" * (33 - len(session_id))
    _table.put_item(Item={
        "PK": f"USER#{user_id}", "SK": "SESSION",
        "sessionId": session_id, "lastActivity": int(time.time()),
    })
    return session_id
