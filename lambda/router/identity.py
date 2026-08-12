"""User identity resolution + allowlist over the DynamoDB identity table.

Identity is `lark:{open_id}` for every entrypoint (webhook + web UI), so a user's
profile/session is shared. Table layout (single-table):
  CHANNEL#lark:{open_id} / PROFILE   -> {userId}
  USER#{userId}          / PROFILE   -> profile
  USER#{userId}          / SESSION   -> {sessionId, lastActivity}
  USER#{userId}          / MEMSESSION-> {memorySessionId, createdAt}
  ALLOW#lark:{open_id}   / ALLOW     -> allowlist entry

The runtime session (which microVM serves a request) and the Memory thread (where
history is stored) are separate ids so they can be rotated independently — see
get_or_create_memory_session.
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


def drop_session(user_id: str) -> None:
    """Forget the stored session id — the next message starts a new AgentCore
    session (and therefore a new runtime instance)."""
    _table.delete_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"})


# When a turn hits a Lark authorization wall, the original message is parked here so
# the shim's /return can replay it once consent completes — the "durable intent"
# that makes callback-driven resume possible without a live task to wake. TTL-bounded
# so an abandoned consent doesn't linger.
_PENDING_AUTH_TTL = int(os.environ.get("PENDING_AUTH_TTL_SECONDS", "600"))  # 10 min


def park_pending_auth(user_id: str, message: str, chat_id: str) -> None:
    """Remember what the user was trying to do, to replay after they authorize.
    One per user (overwrites) — a second attempt supersedes the first."""
    _table.put_item(Item={
        "PK": f"USER#{user_id}", "SK": "PENDING_AUTH",
        "message": message, "chatId": chat_id,
        "createdAt": int(time.time()),
        "ttl": int(time.time()) + _PENDING_AUTH_TTL,
    })


def take_pending_auth(user_id: str) -> dict | None:
    """Return {message, chatId} and delete it — replay is once-only. None if there is
    nothing parked (e.g. the user ran /auth directly, with no message to resume), or
    it expired. A stale item past its ttl is treated as absent even if DynamoDB has
    not swept it yet."""
    item = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PENDING_AUTH"}).get("Item")
    if not item:
        return None
    _table.delete_item(Key={"PK": f"USER#{user_id}", "SK": "PENDING_AUTH"})
    if int(item.get("ttl", 0) or 0) < int(time.time()):
        return None
    return {"message": item.get("message", ""), "chatId": item.get("chatId", "")}


_MEMORY_ID: str | None = None


def _memory_id() -> str:
    """The agent's Memory id, discovered by name prefix (the AgentCore CLI creates
    it as <runtime_name>_mem-<suffix>, so it isn't known at CDK synth time)."""
    global _MEMORY_ID
    if _MEMORY_ID is None:
        prefix = os.environ.get("MEMORY_NAME_PREFIX", "lark_agent")
        ctl = boto3.client("bedrock-agentcore-control", region_name=_REGION)
        ids = [m["id"] for m in ctl.list_memories(maxResults=100).get("memories", [])
               if m["id"].startswith(prefix)]
        _MEMORY_ID = ids[0] if ids else ""
    return _MEMORY_ID


def session_info(user_id: str) -> dict:
    """Current session id + lastActivity, or {} when no session is stored."""
    item = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"}).get("Item") or {}
    return {"sessionId": item.get("sessionId", ""),
            "lastActivity": int(item.get("lastActivity", 0) or 0)}


def get_or_create_memory_session(user_id: str, actor_id: str) -> str:
    """The Memory thread the agent writes history under. Stored separately from the
    runtime session so the two can be rotated independently: /reset rotates only
    this one, /reconnect rotates only the runtime one, /new rotates both. Rotating
    means "use a new id" — no events are deleted."""
    item = _table.get_item(Key={"PK": f"USER#{user_id}", "SK": "MEMSESSION"}).get("Item")
    if item and item.get("memorySessionId"):
        return item["memorySessionId"]
    # Seed with the agent's legacy per-user id so existing history stays visible.
    mem_sid = "sess-" + hashlib.sha256(actor_id.encode()).hexdigest()[:32]
    _table.put_item(Item={"PK": f"USER#{user_id}", "SK": "MEMSESSION",
                          "memorySessionId": mem_sid, "createdAt": int(time.time())})
    return mem_sid


def rotate_memory_session(user_id: str) -> str:
    """Point the user at a brand-new Memory thread (old events are kept, just no
    longer read). Returns the new id."""
    mem_sid = f"sess-{uuid.uuid4().hex}"
    _table.put_item(Item={"PK": f"USER#{user_id}", "SK": "MEMSESSION",
                          "memorySessionId": mem_sid, "createdAt": int(time.time())})
    return mem_sid


_COUNT_PAGE = 100          # ListEvents caps maxResults at 100
_COUNT_MAX_PAGES = 5       # ~500 messages before /status starts reporting "N+"

# Strands writes its session/agent state as blob events carrying a stateType
# metadata key; real conversation events carry no metadata at all. Filtering on
# that server-side is what makes the count exact: maxResults is a *scan* window,
# so unfiltered pages spend their budget on state events and stop early — which is
# why a 70-message thread used to report "67+".
_CONVERSATIONAL_ONLY = {
    "eventMetadata": [{"left": {"metadataKey": "stateType"}, "operator": "NOT_EXISTS"}]
}


def count_events(actor_id: str, session_id: str) -> tuple[int, bool]:
    """(messages, capped) for this Memory thread, counting only conversation events.

    Walks up to _COUNT_MAX_PAGES so ordinary threads report an exact number; capped
    is True only past that, where an exact count isn't worth the API calls."""
    memory_id = _memory_id()
    if not memory_id or not session_id:
        return 0, False
    core = boto3.client("bedrock-agentcore", region_name=_REGION)
    n = 0
    token = None
    for _ in range(_COUNT_MAX_PAGES):
        kwargs = dict(memoryId=memory_id, actorId=actor_id, sessionId=session_id,
                      maxResults=_COUNT_PAGE, filter=_CONVERSATIONAL_ONLY)
        if token:
            kwargs["nextToken"] = token
        page = core.list_events(**kwargs)
        n += sum(1 for ev in page.get("events", [])
                 if any("conversational" in p for p in ev.get("payload") or []))
        token = page.get("nextToken")
        if not token:
            return n, False
    return n, True


# Deleting is one API call per event, so cap it — the caller reports what is left.
_CLEAR_LIMIT = int(os.environ.get("CLEAR_EVENT_LIMIT", "200"))


def clear_history(actor_id: str, session_id: str) -> tuple[int, bool]:
    """Really delete this Memory thread's events (unlike rotating to a new thread,
    which just stops reading the old one). Returns (deleted, more_left)."""
    memory_id = _memory_id()
    if not memory_id or not session_id:
        return 0, False
    core = boto3.client("bedrock-agentcore", region_name=_REGION)
    deleted = 0
    while deleted < _CLEAR_LIMIT:
        # No filter here: /clear removes state events too. Payloads are dead weight
        # when all we need is the eventId.
        page = core.list_events(memoryId=memory_id, actorId=actor_id,
                               sessionId=session_id, maxResults=100,
                               includePayloads=False)
        events = page.get("events", [])
        if not events:
            return deleted, False
        for ev in events:
            if deleted >= _CLEAR_LIMIT:
                return deleted, True
            core.delete_event(memoryId=memory_id, actorId=actor_id,
                              sessionId=session_id, eventId=ev["eventId"])
            deleted += 1
    return deleted, True
