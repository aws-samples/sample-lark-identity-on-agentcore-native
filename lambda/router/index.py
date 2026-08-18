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
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

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

# Leave a margin so the Lambda can still send a reply after a timeout.
READ_TIMEOUT = max(LAMBDA_TIMEOUT - 10, 30)
# `standard` rather than no retries at all. What must never be retried is a read
# timeout — the turn may well have succeeded, and replaying it would duplicate both
# the work and the pushed answer — and standard mode does not retry those (only
# connection errors, throttling and 5xx). Disabling retries outright also gave up on
# those, which are safe and worth retrying.
_RETRIES = {"mode": "standard", "max_attempts": 3}
agentcore = boto3.client(
    "bedrock-agentcore", region_name=AWS_REGION,
    config=Config(read_timeout=READ_TIMEOUT, connect_timeout=10, retries=_RETRIES),
)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)


# ------------------------------- invoke agent -------------------------------

def invoke_agent(session_id: str, user_id: str, actor_id: str, message: str,
                 action: str = "chat", mem_sid: str = "",
                 budget: float | None = None, chat_id: str = "",
                 message_id: str = "", reaction_id: str = "",
                 fresh_session: bool = False) -> dict:
    """Invoke the agent once. Returns the parsed response dict
    {reply, needs_auth, auth_url?} (or {reply:<raw>} on non-JSON).

    `budget` overrides the read timeout for this call — the consent path spends
    part of the Lambda's time waiting for the user, so the retry afterwards must
    fit in what is left, not assume a full budget."""
    payload = json.dumps({
        "action": action, "userId": user_id, "actorId": actor_id,
        "channel": "lark", "message": message,
        # The router owns the Memory thread id (see identity.get_or_create_memory_session).
        "memorySessionId": mem_sid,
        # Where the agent pushes the result when it answers asynchronously.
        "chatId": chat_id,
        # The in-progress reaction this router added, for the agent to remove when
        # the turn ends — only the identity that added one may delete it.
        "messageId": message_id,
        "reactionId": reaction_id,
        # Consent-resume: force a rebuilt session so the new token is used.
        "freshSession": fresh_session,
    }).encode()
    client = agentcore
    if budget is not None:
        client = boto3.client(
            "bedrock-agentcore", region_name=AWS_REGION,
            config=Config(read_timeout=max(int(budget), 10), connect_timeout=10,
                          retries=_RETRIES),
        )
    resp = client.invoke_agent_runtime(
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


def _app_identity() -> str:
    """The Lark app the bot speaks as. Shown next to the user identity because this
    sample's whole point is that the two are different: tools reach Lark as the
    *user* (their vaulted token), while replies, reactions and cards go out as the
    *app* (its tenant token). Masked — an appId is not a secret, but there is no
    reason to paste a full identifier into a chat."""
    app_id = lark.get_credentials()[0]
    if not app_id:
        return "未配置"
    return f"{app_id[:8]}…{app_id[-4:]}" if len(app_id) > 14 else app_id


# ------------------------- execution environment probe -----------------------

# Measured: probing a session id with no live microVM provisions one (a fresh id
# answered in 1.4 s), so this is NOT a passive read — /status materialises what it
# reports. That is a fair trade for making turnover visible, but it does mean the
# id shown may have been created by the command itself. Budget is kept short so a
# slow cold start degrades to "unknown" instead of stalling the command.
_PROBE_SECONDS = int(os.environ.get("STATUS_PROBE_SECONDS", "8"))


def _microvm_line(session_id: str, user_id: str, actor_id: str) -> str:
    """One line describing the microVM currently bound to this session id.

    AgentCore's terms: a session (keyed by runtimeSessionId) is served by a
    dedicated execution environment, realised as a microVM. The mapping is 1:1 but
    not permanent — once the microVM is terminated (idleRuntimeSessionTimeout,
    default 15 min; maxLifetime, default 8 h; both configurable — or
    StopRuntimeSession; or a failed health check) the same session id gets a brand
    new microVM with sanitized memory, not the old one back. The session id does not
    change then, which is why it alone can't show this and the microVM reports its
    own id (agent/server.py:_INSTANCE)."""
    if not session_id:
        return "无（尚未建立会话）"
    data = None
    for attempt in range(2):
        try:
            data = invoke_agent(session_id, user_id, actor_id, "",
                                action="status", budget=_PROBE_SECONDS)
            break
        except Exception as e:  # noqa: BLE001 — diagnostics must not fail the command
            # A 409 RetryableConflictException means the service is mid-provision for
            # this session; AWS documents a short backoff. Retried only here: the chat
            # path deliberately disables retries so a timeout can't replay a turn.
            retryable = "RetryableConflict" in type(e).__name__ or "409" in str(e)
            if retryable and attempt == 0:
                logger.info("microVM probe conflicted, retrying once")
                time.sleep(1)
                continue
            logger.info("microVM probe failed for %s: %s", actor_id, type(e).__name__)
            return "未知（探测未返回；下条消息仍会正常处理）"
    inst = data.get("instance")
    if not inst:
        return "运行中（旧镜像，未上报实例信息）"
    # Two distinct figures, both meaningful; the process's own uptime is neither, and
    # is deliberately not shown. kernelUptime is the microVM's age (from the kernel,
    # the only trustworthy source); sessionAge is how long it has served this session.
    parts = []
    kup = data.get("kernelUptime")
    if kup is not None:
        parts.append(f"已运行 {_secs(kup)}")
    age = data.get("sessionAge")
    if age is not None:
        parts.append(f"服务本会话 {_secs(age)}")
    return f"{inst}（{'，'.join(parts)}）" if parts else f"{inst}（运行中）"


def _secs(seconds) -> str:
    """Always seconds, never minutes. The two figures shown together are meant to be
    compared (microVM age vs. how long it has served this session), and mixed units
    make that arithmetic awkward — this is a lifecycle demo, not a status page."""
    return f"{int(float(seconds))} 秒"


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


# ----------------------------- consent resume -------------------------------

def resume_consented_turn(actor_id: str) -> None:
    """Replay the message that hit an auth wall, now that the user has consented.
    Invoked by the shim's /return after 3LO completes — this is the callback-driven
    resume that lets the task continue without the user re-sending.

    No-op if nothing was parked (the user may have run /auth directly, with no turn
    to resume) or it expired."""
    if not actor_id.startswith("lark:"):
        logger.info("resume: unexpected actor_id %r", actor_id)
        return
    open_id = actor_id.split(":", 1)[1]
    user_id, _ = identity.resolve_user("lark", open_id)
    if not user_id:
        logger.info("resume: no user for %s", actor_id)
        return
    parked = identity.take_pending_auth(user_id)
    if not parked:
        logger.info("resume: nothing parked for %s", actor_id)
        return
    message, chat_id = parked["message"], parked["chatId"]
    logger.info("resume: replaying for %s: %r", actor_id, message[:80])
    session_id = identity.get_or_create_session(user_id)
    mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
    # A fresh reaction on the resumed turn is not possible (the original message id
    # isn't parked), so none is passed — the answer arrives without a marker.
    invoke_agent(session_id, user_id, actor_id, message,
                 action="chat_async", mem_sid=mem_sid, chat_id=chat_id,
                 fresh_session=True)


# ---------------------------- approval events -------------------------------

# Both arrive in the legacy 1.0 schema. `approval_task` is the actionable one: it names
# an approver (open_id) and the task, which is exactly what a decision needs.
# `approval_instance` reports the instance's own status and is accepted only so the log
# shows it was seen — deciding from it would take another call to learn whose task it is.
_APPROVAL_EVENT_TYPES = {"approval_task", "approval_instance"}


def _approval_prompt(instance_code: str, task_id: str, open_id: str,
                     approval_code: str) -> str:
    """The turn the agent wakes up to. The ids are handed over rather than left to be
    discovered: an event-driven turn has no user to ask, and `user_id` decides whose
    name the decision is recorded under — too consequential to let the model guess."""
    return "\n".join([
        "【审批事件】有一条待审批任务分配给了你，请代为处理。",
        f"approval_code: {approval_code}",
        f"instance_code: {instance_code}",
        f"task_id: {task_id}",
        f"user_id（审批归属人，就是你）: {open_id}",
        "",
        "请先查看审批详情，判断这件事本身是否成立：内容是否完整、是否与申报事由相符、有无异常。",
        "然后直接做出批准或拒绝，并说明理由。",
        "是否允许自动决定由审批工具在代码里判定 —— 不要自己揣测权限范围而放弃处理；",
        "如果工具拒绝，把它给出的原因转述给我即可。",
    ])


def process_approval_event(ev: dict, context=None) -> None:
    """Event-driven approval: a task lands, the agent decides it with nobody present.

    Which definitions reach here is already decided by what we subscribed to
    (scripts/subscribe-approvals.sh), and whether a decision is *allowed* is enforced
    in the approval MCP server. So this function deliberately re-checks neither — it
    only establishes that there is a real pending task, for a known user, once."""
    # Logged whole: the payload shape for these events is thinly documented, so this is
    # the ground truth for whoever extends it next.
    logger.info("approval event: %s", json.dumps(ev, ensure_ascii=False)[:900])

    status = str(ev.get("status", "")).upper()
    # `open_id` appears in the doc's sample payload but not in its field table, which
    # documents only `user_id` ("operator id", and empty on auto-approve tasks) — in the
    # tenant user_id format, not the open_id this project keys identity on. So open_id
    # is what we need and the less documented of the two. Guessing wrong fails closed:
    # an id that isn't the approver's resolves to no allowlisted user, or to someone
    # with no vaulted grant, and the approval server refuses either way.
    open_id = str(ev.get("open_id", "") or "")
    task_id = str(ev.get("task_id", "") or "")
    instance_code = str(ev.get("instance_code", "") or "")
    approval_code = str(ev.get("approval_code", "") or "")

    # Only a task still awaiting a decision is actionable. This is also what stops the
    # obvious loop: the agent's own approve emits another event, with a settled status.
    if status != "PENDING":
        logger.info("approval: status=%s — nothing to decide", status or "(none)")
        return
    if not (open_id and task_id and instance_code):
        logger.info("approval: no per-approver task in this event, skipping")
        return
    # Lark redelivers until acked, and the ack goes out long before the agent decides.
    if not identity.claim_approval_task(task_id):
        logger.info("approval: task %s already claimed — redelivery", task_id)
        return

    actor_id = f"lark:{open_id}"
    user_id, _ = identity.resolve_user("lark", open_id)
    if not user_id:
        # Someone outside the demo's allowlist. Their approval is their own business —
        # staying silent is the right move, not an error.
        logger.info("approval: %s not in the allowlist, leaving it alone", actor_id)
        return

    message = _approval_prompt(instance_code, task_id, open_id, approval_code)
    # No chat here — an approval event carries none — so the approver's open_id is the
    # delivery address, which the senders read as "DM this person".
    if not user_token_vaulted(actor_id):
        # The approval server refuses to decide without this person's own grant, so the
        # turn will wall. Park it, and consent-resume replays it once they authorize.
        identity.park_pending_auth(user_id, message, open_id)
    logger.info("approval: dispatching task %s for %s", task_id, actor_id)
    _dispatch_turn(user_id, actor_id, message, open_id, context=context)


# ------------------------------- async processing ---------------------------

def process_lark_event(body: str, headers: dict, context=None) -> None:
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
    # Message events use schema 2.0 (type in `header`); approval events still use 1.0,
    # where the type sits inside `event`. Reading both is what lets one webhook URL
    # serve both kinds.
    event_type = header.get("event_type") or event.get("type", "")
    logger.info("event_type=%s", event_type)
    if event_type in _APPROVAL_EVENT_TYPES:
        process_approval_event(event, context)
        return
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
    message_id = message.get("message_id", "")
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
    # /status — read-only diagnostics over the three independent dimensions: the
    # session id that routes you, the container currently serving that id, and the
    # Memory thread holding your history.
    if cmd == "/status":
        info = identity.session_info(user_id)
        rt_sid = info.get("sessionId", "")
        mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
        events, capped = identity.count_events(actor_id, mem_sid)
        last = info.get("lastActivity", 0)
        last_str = (datetime.datetime.fromtimestamp(last, datetime.timezone.utc)
                    .strftime("%Y-%m-%d %H:%M UTC") if last else "—")
        lines = [
            f"应用身份：{_app_identity()}",
            f"用户身份：{actor_id}",
            f"会话路由键：{rt_sid or '尚未建立（发一条普通消息后创建）'}",
            f"当前 microVM：{_microvm_line(rt_sid, user_id, actor_id)}",
            f"记忆线程：{mem_sid}",
            f"该线程对话记录：{events}{'+' if capped else ''} 条",
            f"最近活跃：{last_str}",
            "授权状态：发送 /auth 查看",
        ]
        lark.send_message(chat_id, "\n".join(lines))
        logger.info("status for %s: events=%d", actor_id, events)
        return

    # If the user isn't authorized yet, a Lark tool this turn may hit an auth wall
    # deep in the async run — past where the router can see it. Park the message now
    # so the shim's /return can replay it once consent lands. Only for unauthorized
    # users: an authorized turn won't wall, and parking every message would be waste.
    # Left to expire by TTL if this turn needs no Lark tool after all.
    if not user_token_vaulted(actor_id):
        identity.park_pending_auth(user_id, agent_message, chat_id)
    _dispatch_turn(user_id, actor_id, agent_message, chat_id, message_id, context)


def _dispatch_turn(user_id: str, actor_id: str, agent_message: str, chat_id: str,
                   message_id: str = "", context=None) -> None:
    """Invoke the agent for one turn and deliver the reply. Shared by the webhook
    path and the consent-resume path (shim replays a parked message here)."""
    session_id = identity.get_or_create_session(user_id)
    mem_sid = identity.get_or_create_memory_session(user_id, actor_id)
    logger.info("invoking agent: session=%s mem=%s msg=%r",
                session_id, mem_sid, agent_message[:80])
    # Acknowledge before doing anything slow: the first token is seconds away
    # (session assembly, MCP handshake, model latency), and until then the user has
    # no way to tell "working on it" from "my message never arrived". The agent
    # removes this when the turn ends.
    reaction_id = lark.add_reaction(message_id)
    try:
        # chat_async: the agent accepts the work, returns at once, and pushes the
        # answer to the chat itself. A synchronous wait cannot cover real tasks —
        # both InvokeAgentRuntime and this Lambda cap out long before they finish.
        result = invoke_agent(session_id, user_id, actor_id, agent_message,
                              action="chat_async", mem_sid=mem_sid, chat_id=chat_id,
                              message_id=message_id, reaction_id=reaction_id)
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
                # Claim the parked turn before replaying it. The shim's callback races
                # us the moment consent lands — it claims and replays too — so whoever
                # polls the token first would otherwise run the turn a second time, in
                # a different session, with the side effects duplicated. Whoever wins
                # the claim runs it; the loser stops here.
                if identity.take_pending_auth(user_id) is None:
                    logger.info("consent complete for %s; already claimed elsewhere",
                                actor_id)
                    return
                logger.info("consent complete for %s; re-invoking", actor_id)
                # Waiting for consent already spent part of the budget.
                left = (context.get_remaining_time_in_millis() / 1000 - 10
                        if context else READ_TIMEOUT)
                result = invoke_agent(session_id, user_id, actor_id, agent_message,
                                      action="chat_async", mem_sid=mem_sid,
                                      budget=left, chat_id=chat_id,
                                      message_id=message_id, reaction_id=reaction_id,
                                      fresh_session=True)
            else:
                logger.info("consent wait timed out for %s", actor_id)
                return  # link already sent; user finishes later and re-sends
        if result.get("accepted"):
            logger.info("agent accepted the turn for %s; it will push the reply", actor_id)
            return
        reply = result.get("reply", "")
    except ReadTimeoutError:
        # We stopped waiting; the agent keeps running and may still finish, so
        # don't report failure — the doc it was writing might well exist.
        logger.warning("agent read timeout for %s", actor_id)
        reply = ("That took longer than I can wait for. It may still have "
                 "completed — please check, or try a smaller request.")
    except Exception as e:  # noqa: BLE001
        logger.exception("agent invocation failed")
        reply = f"Sorry, something went wrong ({type(e).__name__})."

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
        process_lark_event(event["body"], event.get("headers", {}), context)
        return {"ok": True}

    # Consent-resume path: the shim invokes us here after a user finishes 3LO, so the
    # message that hit the auth wall can be replayed with the token now in the vault
    # — the user does not re-send. See .dev/adr and PLAN-consent-resume.
    if event.get("_consent_resumed"):
        actor_id = event.get("actorId", "")
        resume_consented_turn(actor_id)
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
