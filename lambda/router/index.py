"""Router Lambda — Lark webhook ingestion.

Sync path (API Gateway): handle url_verification challenge, verify signature,
then self-invoke asynchronously and return 200 immediately (avoids webhook
timeout). Async path: decrypt + parse the event, resolve the user, invoke the
AgentCore Runtime, and send the reply back to the Lark chat.

Identity: lark:{open_id} — the same identity the web UI resolves to.
"""

from __future__ import annotations

import json
import logging
import os
import re

import boto3
from botocore.config import Config

import lark
import identity

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT")
SELF_FUNCTION_NAME = os.environ.get("SELF_FUNCTION_NAME", os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""))
LAMBDA_TIMEOUT = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "60"))

_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_\-.]{1,200}$")

agentcore = boto3.client(
    "bedrock-agentcore", region_name=AWS_REGION,
    config=Config(read_timeout=max(LAMBDA_TIMEOUT - 10, 30), connect_timeout=10,
                  retries={"max_attempts": 0}),
)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)


# ------------------------------- invoke agent -------------------------------

def invoke_agent(session_id: str, user_id: str, actor_id: str, message: str) -> str:
    payload = json.dumps({
        "action": "chat", "userId": user_id, "actorId": actor_id,
        "channel": "lark", "message": message,
    }).encode()
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN, qualifier=QUALIFIER,
        runtimeSessionId=session_id, runtimeUserId=actor_id,
        payload=payload, contentType="application/json", accept="application/json",
    )
    raw = resp["response"].read().decode() if hasattr(resp["response"], "read") else resp["response"]
    try:
        data = json.loads(raw)
    except Exception:
        return raw or ""
    return data.get("reply", data.get("error", ""))


# ------------------------------- async processing ---------------------------

def process_lark_event(body: str, headers: dict) -> None:
    """Runs in the async self-invocation. Decrypt (if needed), handle message."""
    try:
        event_data = json.loads(body)
    except Exception:
        logger.error("async: invalid body")
        return

    # decrypt if encrypted
    if "encrypt" in event_data and "header" not in event_data:
        decrypted = lark.decrypt_event(event_data["encrypt"])
        if decrypted is None:
            logger.error("async: decryption failed")
            return
        event_data = decrypted

    header = event_data.get("header", {})
    event = event_data.get("event", {})
    event_type = header.get("event_type")
    logger.info("event_type=%s", event_type)
    if event_type != "im.message.receive_v1":
        logger.info("ignoring non-message event")
        return

    sender = event.get("sender", {})
    if sender.get("sender_type") != "user":
        logger.info("ignoring non-user sender")
        return
    open_id = sender.get("sender_id", {}).get("open_id")
    message = event.get("message", {})
    chat_id = message.get("chat_id")
    msg_type = message.get("message_type")
    content_str = message.get("content", "{}")
    logger.info("message from open_id=%s chat_id=%s type=%s", open_id, chat_id, msg_type)
    if not (open_id and chat_id):
        return

    # extract text
    try:
        content = json.loads(content_str)
    except Exception:
        content = {}
    text = content.get("text", "") if msg_type == "text" else content.get("text", "")

    # strip @mentions in group chats
    if message.get("chat_type") == "group":
        for m in message.get("mentions", []) or []:
            text = text.replace(m.get("key", ""), "").strip()

    actor_id = f"lark:{open_id}"
    user_id, is_new = identity.resolve_user("lark", open_id)
    logger.info("resolve_user -> user_id=%s is_new=%s", user_id, is_new)
    if user_id is None:
        logger.info("user not allowed: %s", actor_id)
        lark.send_message(
            chat_id,
            f"You are not authorized yet. Your ID: {actor_id}. "
            f"Share it with the admin to request access.",
        )
        return

    agent_message = text.strip() or "hi"
    session_id = identity.get_or_create_session(user_id)
    logger.info("invoking agent: session=%s msg=%r", session_id, agent_message[:80])
    try:
        reply = invoke_agent(session_id, user_id, actor_id, agent_message)
    except Exception as e:  # noqa: BLE001
        logger.exception("agent invocation failed")
        reply = f"Sorry, something went wrong: {e}"

    if reply:
        lark.send_message(chat_id, reply)


# ------------------------------- handler ------------------------------------

def _resp(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def handler(event, context):
    # Async self-invocation path
    if event.get("_async_dispatch"):
        logger.info("async dispatch: processing lark event")
        process_lark_event(event["body"], event.get("headers", {}))
        return {"ok": True}

    path = event.get("rawPath", event.get("requestContext", {}).get("http", {}).get("path", ""))
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    headers = event.get("headers", {}) or {}
    body = event.get("body", "") or ""
    logger.info("webhook hit: method=%s path=%s bytes=%d", method, path, len(body))

    if path.endswith("/health"):
        return _resp(200, {"status": "ok"})

    if not path.endswith("/webhook/lark"):
        return _resp(404, {"error": "not found"})

    # url_verification challenge (handled synchronously, may be encrypted)
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = {}
    if "encrypt" in parsed and "type" not in parsed:
        decrypted = lark.decrypt_event(parsed["encrypt"])
        parsed = decrypted or {}
    if parsed.get("type") == "url_verification":
        challenge = parsed.get("challenge", "")
        if not _CHALLENGE_RE.match(challenge):
            return _resp(400, {"error": "invalid challenge"})
        return _resp(200, {"challenge": challenge})

    # verify signature (fail-closed)
    if not lark.verify_signature(headers, body.encode()):
        return _resp(401, {"error": "invalid signature"})

    # dispatch async and ack immediately
    try:
        lambda_client.invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps({"_async_dispatch": True, "body": body,
                                "headers": {k: v for k, v in headers.items()
                                            if k.lower().startswith("x-lark-")}}).encode(),
        )
    except Exception:
        logger.exception("async dispatch failed")
    return _resp(200, {"ok": True})
