"""User resolution + session over the shared DynamoDB identity table.

Same identity model as the router: `lark:{open_id}`. This is a copy of the
router's identity helpers (Lambda asset dirs can't share modules). Keep the two
in sync — the layout must match so both entrypoints resolve the same user.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid

import boto3

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
_REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"

_table = boto3.resource("dynamodb", region_name=_REGION).Table(_TABLE_NAME)


def is_user_allowed(channel: str, channel_user_id: str) -> bool:
    if _REGISTRATION_OPEN:
        return True
    key = f"{channel}:{channel_user_id}"
    return "Item" in _table.get_item(Key={"PK": f"ALLOW#{key}", "SK": "ALLOW"})


def resolve_user(channel: str, channel_user_id: str, display_name: str = "") -> tuple[str | None, bool]:
    channel_key = f"{channel}:{channel_user_id}"
    resp = _table.get_item(Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"})
    if "Item" in resp:
        return resp["Item"]["userId"], False
    if not is_user_allowed(channel, channel_user_id):
        return None, False
    user_id = "user_" + hashlib.sha256(channel_key.encode()).hexdigest()[:16]
    now = int(time.time())
    _table.put_item(Item={"PK": f"USER#{user_id}", "SK": "PROFILE",
                          "displayName": display_name, "createdAt": now})
    _table.put_item(Item={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE",
                          "userId": user_id, "channel": channel})
    _table.put_item(Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                          "channel": channel})
    return user_id, True


def get_or_create_session(user_id: str) -> str:
    resp = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"})
    if "Item" in resp:
        _table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": "SESSION"},
            UpdateExpression="SET lastActivity = :t",
            ExpressionAttributeValues={":t": int(time.time())},
        )
        return resp["Item"]["sessionId"]
    session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
    if len(session_id) < 33:
        session_id += "0" * (33 - len(session_id))
    _table.put_item(Item={"PK": f"USER#{user_id}", "SK": "SESSION",
                          "sessionId": session_id, "lastActivity": int(time.time())})
    return session_id
