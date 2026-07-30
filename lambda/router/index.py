"""Router Lambda — Lark webhook ingestion.

Sync path (API Gateway): handle url_verification challenge, verify signature,
then self-invoke asynchronously and return 200 immediately (avoids webhook
timeout). Async path: decrypt + parse the event, resolve the user, invoke the
AgentCore Runtime, and send the reply back to the Lark chat.

Identity: lark:{open_id} — the same identity the web UI resolves to.
"""

from __future__ import annotations

import datetime
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

def invoke_agent(session_id: str, user_id: str, actor_id: str, message: str,
                 action: str = "chat", mem_sid: str = "") -> dict:
    """Invoke the agent once. Returns the parsed response dict
    {reply, needs_auth, auth_url?} (or {reply:<raw>} on non-JSON)."""
    payload = json.dumps({
        "action": action, "userId": user_id, "actorId": actor_id,
        "channel": "lark", "message": message,
        # The router owns the Memory thread id (see identity.get_or_create_memory_session).
        "memorySessionId": mem_sid,
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
        return {"reply": raw or ""}
    if "reply" not in data and "error" in data:
        data["reply"] = data["error"]
    return data


# ------------------------------- consent wait -------------------------------

# How long the async Lambda holds, waiting for the user to finish 3LO consent.
# Bounded by the Lambda timeout (see the agentcore read_timeout above); on
# timeout we fall back to "send your message again".
AUTH_WAIT_SECONDS = int(os.environ.get("AUTH_WAIT_SECONDS", "45"))
AUTH_POLL_INTERVAL = float(os.environ.get("AUTH_POLL_INTERVAL", "2"))
LARK_OAUTH_PROVIDER = os.environ.get("LARK_OAUTH_PROVIDER", "lark-agent-3lo")
AGENT_WORKLOAD_NAME = os.environ.get("AGENT_WORKLOAD_NAME", "lark-agent-wl")
LARK_SCOPES = os.environ.get("LARK_SCOPES", "drive:drive docx:document offline_access").split()
SHIM_RETURN_URL = os.environ.get("SHIM_RETURN_URL", "")  # required by GetResourceOauth2Token

# One runtime can front several IdPs — one OAuth provider per downstream system.
# IDP_REGISTRY is a JSON list of {key, provider, scopes, label}; `key` is what the
# user types (/auth lark). Falls back to the single-provider env vars.
def _load_idps() -> dict:
    raw = os.environ.get("IDP_REGISTRY", "").strip()
    if raw:
        try:
            return {i["key"]: i for i in json.loads(raw)}
        except Exception:
            logger.exception("bad IDP_REGISTRY, falling back to single provider")
    return {"lark": {"key": "lark", "provider": LARK_OAUTH_PROVIDER,
                     "scopes": LARK_SCOPES, "label": "Lark"}}


IDPS = _load_idps()


def user_token_vaulted(actor_id: str, idp_key: str = "lark") -> bool:
    """True once this user's token for `idp_key` is in the Token Vault (consent
    complete). Same USER_FEDERATION sequence the agent uses; presence check only.
    ResourceOauth2ReturnUrl is required even for a presence check — without a
    valid token AgentCore refuses the call rather than returning empty."""
    idp = IDPS.get(idp_key)
    if not idp:
        return False
    try:
        wat = agentcore.get_workload_access_token_for_user_id(
            workloadName=AGENT_WORKLOAD_NAME, userId=actor_id
        )["workloadAccessToken"]
        kwargs = dict(
            workloadIdentityToken=wat,
            resourceCredentialProviderName=idp["provider"],
            scopes=idp["scopes"],
            oauth2Flow="USER_FEDERATION",
        )
        if SHIM_RETURN_URL:
            kwargs["resourceOauth2ReturnUrl"] = SHIM_RETURN_URL
        resp = agentcore.get_resource_oauth2_token(**kwargs)
        has = bool(resp.get("accessToken"))
        logger.info("vault check %s (%s): token=%s authUrl=%s", actor_id, idp_key,
                    has, bool(resp.get("authorizationUrl")))
        return has
    except Exception:
        logger.exception("vault check failed for %s (%s)", actor_id, idp_key)
        return False


def wait_for_consent(actor_id: str) -> bool:
    """Poll the vault until the token appears or AUTH_WAIT_SECONDS elapses."""
    import time
    deadline = time.monotonic() + AUTH_WAIT_SECONDS
    while time.monotonic() < deadline:
        if user_token_vaulted(actor_id):
            return True
        time.sleep(AUTH_POLL_INTERVAL)
    return False


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

    cmd = agent_message.lower()
    if cmd in ("/help", "/?"):
        lark.send_message(chat_id, "\n".join([
            "可用命令：",
            "  /auth        查看各 IdP 的授权状态",
            "  /auth <idp>  对该 IdP 授权或重新授权",
            "  /status      当前身份、会话与对话记录",
            "  /new         开启新的对话（切换运行实例）",
            "  /reset       重置对话记录（运行实例不变）",
            "  /clear       清除对话记录（运行实例不变）",
            "  /reconnect   切换运行实例（对话记录保留）",
        ]))
        return
    # The runtime session (which microVM serves you) and the Memory thread (your
    # conversation history) are independent ids.
    #
    # /reset — same runtime instance, new Memory thread (history starts over).
    if cmd == "/reset":
        mem_sid = identity.rotate_memory_session(user_id)
        logger.info("memory session rotated for %s -> %s", actor_id, mem_sid)
        lark.send_message(chat_id, "已开始新的对话记录（运行实例不变）。")
        return
    # /new — new runtime instance AND new Memory thread: a fully fresh start.
    if cmd == "/new":
        identity.drop_session(user_id)
        mem_sid = identity.rotate_memory_session(user_id)
        logger.info("runtime + memory session rotated for %s -> %s", actor_id, mem_sid)
        lark.send_message(chat_id, "已开启新会话：新的对话记录，且由新的运行实例处理。")
        return
    # /clear — actually delete this thread's events. Different from /reset, which
    # just starts a new thread and leaves the old data in place.
    if cmd == "/clear":
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        n, more = identity.clear_history(actor_id, mem_sid)
        logger.info("history deleted for %s: %d events (more=%s)", actor_id, n, more)
        lark.send_message(
            chat_id, f"已删除对话记录 {n} 条{'（仍有剩余，可再执行一次 /clear）' if more else ''}。")
        return
    # /reconnect — new runtime instance, same Memory thread. Demonstrates that
    # AgentCore Memory outlives the container: a fresh microVM still remembers.
    if cmd == "/reconnect":
        identity.drop_session(user_id)
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        n, capped = identity.count_events(actor_id, mem_sid)
        logger.info("runtime session dropped (memory kept) for %s", actor_id)
        lark.send_message(
            chat_id, f"已切换运行实例，对话记录保留（{n}{'+' if capped else ''} 条）"
                     "——记忆存放在 AgentCore Memory，不随容器生命周期消失。")
        return
    # /auth [idp] — authorization is per-IdP (one OAuth provider per downstream
    # system). Bare /auth lists each IdP's status; /auth <idp> starts a fresh 3LO
    # flow for that one (idempotent — each run hands out a new consent link).
    if cmd == "/auth" or cmd.startswith("/auth "):
        arg = agent_message[5:].strip().lower()
        if not arg:
            lines = ["各 IdP 的授权状态："]
            for key, idp in IDPS.items():
                ok = user_token_vaulted(actor_id, key)
                lines.append(f"  {'✅' if ok else '❌'} {key} ({idp.get('label', key)})"
                             f"{'' if ok else ' — 发送 /auth ' + key + ' 授权'}")
            lark.send_message(chat_id, "\n".join(lines))
            return
        if arg not in IDPS:
            lark.send_message(
                chat_id, f"未知的 IdP：{arg}。可用：{', '.join(IDPS) or '（未配置）'}")
            return
        session_id = identity.get_or_create_session(user_id)
        result = invoke_agent(session_id, user_id, actor_id, arg, action="reauth")
        auth_url = result.get("auth_url")
        if auth_url:
            label = IDPS[arg].get("label", arg)
            lark.send_link_message(
                chat_id, f"请授权访问你的 {label} 账号：", "点击授权", auth_url)
            logger.info("forced re-auth for %s (idp=%s)", actor_id, arg)
        else:
            lark.send_message(chat_id, result.get("reply") or result.get("error", "无法发起授权"))
        return
    # /status — read-only diagnostics. Shows BOTH ids so the two dimensions
    # (which microVM serves you vs. which Memory thread holds your history) are
    # visible and their independence is obvious.
    if cmd == "/status":
        info = identity.session_info(user_id)
        rt_sid = info.get("sessionId", "")
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        events, capped = identity.count_events(actor_id, mem_sid)
        last = info.get("lastActivity", 0)
        last_str = (datetime.datetime.fromtimestamp(last, datetime.timezone.utc)
                    .strftime("%Y-%m-%d %H:%M UTC") if last else "—")
        lines = [
            f"身份：{actor_id}",
            f"运行实例会话：{rt_sid or '尚未建立（发一条普通消息后创建）'}",
            f"记忆线程：{mem_sid}",
            f"该线程对话记录：{events}{'+' if capped else ''} 条",
            f"最近活跃：{last_str}",
            "授权状态：发送 /auth 查看",
        ]
        lark.send_message(chat_id, "\n".join(lines))
        logger.info("status for %s: events=%d", actor_id, events)
        return

    session_id = identity.get_or_create_session(user_id)
    mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
    logger.info("invoking agent: session=%s mem=%s msg=%r",
                session_id, mem_sid, agent_message[:80])
    try:
        result = invoke_agent(session_id, user_id, actor_id, agent_message, mem_sid=mem_sid)
        # First-use 3LO: post the consent link, then hold and poll the vault so
        # the user gets an answer without re-sending (bounded by AUTH_WAIT_SECONDS).
        if result.get("needs_auth"):
            auth_url = result.get("auth_url")
            if auth_url:
                lark.send_link_message(
                    chat_id, "需要访问你的 Lark 账号，请先授权：", "点击授权", auth_url)
            else:  # no structured url — fall back to the agent's text
                lark.send_message(chat_id, result.get("reply", ""))
            logger.info("awaiting consent for %s (<= %ds)", actor_id, AUTH_WAIT_SECONDS)
            if wait_for_consent(actor_id):
                logger.info("consent complete for %s; re-invoking", actor_id)
                result = invoke_agent(session_id, user_id, actor_id, agent_message,
                                      mem_sid=mem_sid)
            else:
                logger.info("consent wait timed out for %s", actor_id)
                return  # link already sent; user finishes later and re-sends
        reply = result.get("reply", "")
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
