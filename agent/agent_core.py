"""Core agent: Strands Agent on Bedrock, with AgentCore Memory for session
continuity and an MCP Gateway client for per-user tool identity pass-through.

Server-reuse model (see AWS AgentCore + Strands guidance): the model, the MCP
client, and the agent are built ONCE per session and cached — not rebuilt per
message. Rebuilding per message re-handshakes the Gateway and re-lists tools
every time, which adds ~15–20s of latency. AgentCore gives each session its own
microVM, so the cache holds essentially one entry per container.

Memory: AgentCoreMemorySessionManager with batch_size=1 persists each turn to
Memory immediately (STM), so history survives idle-termination + a new microVM,
and is keyed by (actor_id, session_id) — one long thread per user.

Per-user Lark access (agent-side 3LO): for each end-user the agent fetches that
user's vaulted Lark token from AgentCore Identity (GetResourceOauth2Token,
USER_FEDERATION) and connects directly to the lark-mcp Runtime, passing the token
in a custom header (the lark-mcp sidecar copies it to Authorization). If the user
hasn't consented yet, the agent replies with the 3LO auth link instead (see
lark_3lo.py). We drive 3LO from the agent because the Gateway does not do per-user
3LO injection for a CustomOauth2 provider (AWS gap, agentcore-samples#1424).

`run_chat` returns the final text; `stream_chat` yields text deltas.
"""

from __future__ import annotations

import hashlib
import os
import time
import logging
import threading
from typing import Iterator

from strands import Agent
from strands.models import BedrockModel

import lark_3lo

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
_SYSTEM = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful assistant embedded in Lark. Be concise. "
    "Use the provided tools when they help answer the user.",
)
# Rebuild a cached session before its Cognito access token (~1h) expires.
_SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", "3000"))  # 50 min

_model = BedrockModel(model_id=_MODEL_ID, streaming=True)

# session_id -> {agent, mcp, created}. One microVM ≈ one session, so this is tiny.
_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _session_id_for(actor_id: str) -> str:
    """Deterministic per-user session id: one long conversation thread per user,
    shared across reconnects and entrypoints (STM retains it 30 days)."""
    return "sess-" + hashlib.sha256(actor_id.encode()).hexdigest()[:32]


def _make_session_manager(actor_id: str, session_id: str):
    """AgentCore Memory (STM) session manager, or None if Memory isn't configured.
    batch_size=1 → each turn is sent to Memory immediately (no close() needed)."""
    if not _MEMORY_ID:
        return None
    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import (
        AgentCoreMemorySessionManager,
    )
    cfg = AgentCoreMemoryConfig(
        memory_id=_MEMORY_ID, session_id=session_id, actor_id=actor_id, batch_size=1,
    )
    return AgentCoreMemorySessionManager(cfg, region_name=_REGION)


def _build_session(actor_id: str, email: str, mem_sid: str) -> dict:
    """Build a session for this user. Agent-side 3LO: if the user's Lark token is
    vaulted, open an MCP client to lark-mcp (SigV4 + token in the custom header)
    and list tools; if not, carry the auth_url so run_chat can prompt the user.
    The MCP client is entered once and kept open for the session."""
    mcp = None
    tools = []
    auth_url = None
    identity_error = None
    try:
        kind, value = lark_3lo.get_user_lark_token(actor_id)
    except Exception as e:  # noqa: BLE001 — never crash the turn on identity hiccups
        log.exception("3LO token lookup failed for %s", actor_id)
        kind, value = "error", str(e)

    if kind == "token":
        mcp = lark_3lo.mcp_client_for(value)
        mcp.__enter__()  # persistent connection for the session's lifetime
        tools = mcp.list_tools_sync()
    elif kind == "auth_url":
        auth_url = value  # no tools this session; user must consent first
    else:
        # Surface identity failures instead of running tool-less: an agent that
        # merely says "I have no tools" reads as model behaviour and hides the
        # real cause (a missing IAM permission, most likely).
        identity_error = value

    agent = Agent(
        model=_model, system_prompt=_SYSTEM, tools=tools,
        session_manager=_make_session_manager(actor_id, mem_sid),
    )
    return {"agent": agent, "mcp": mcp, "created": time.time(),
            "auth_url": auth_url, "identity_error": identity_error}


_AUTH_PROMPT = (
    "To do that I need access to your Lark account. Please authorize once here, "
    "then send your message again:\n{url}"
)

_IDENTITY_ERROR = (
    "I can't reach your Lark account right now, so I have no Lark tools this turn "
    "(other questions still work).\nDetails: {err}"
)


def _get_session(actor_id: str, email: str, mem_sid: str) -> dict:
    """Return the cached session for this user, rebuilding it if absent or near
    token expiry. A pending-authorization session (no token yet) is NOT cached —
    so the next turn re-checks the vault and picks up a freshly consented token."""
    cache_key = f"{actor_id}|{mem_sid}"
    with _lock:
        s = _sessions.get(cache_key)
        if s and not s.get("auth_url") and (time.time() - s["created"]) < _SESSION_TTL:
            return s
        if s and s.get("mcp"):
            try:
                s["mcp"].__exit__(None, None, None)  # close the stale connection
            except Exception:
                pass
        s = _build_session(actor_id, email, mem_sid)
        if not s.get("auth_url") and not s.get("identity_error"):
            _sessions[cache_key] = s  # cache only fully working sessions
        return s


def chat_result(actor_id: str, message: str, email: str = "",
                mem_sid: str = "") -> dict:
    """Non-streaming chat → {reply, needs_auth, auth_url}. History via Memory.
    When the user hasn't authorized Lark yet, needs_auth is True and auth_url is
    the raw consent URL, so the caller (router) can drive the wait-for-consent
    loop instead of asking the user to re-send."""
    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id))
    if s.get("auth_url"):
        return {"reply": _AUTH_PROMPT.format(url=s["auth_url"]),
                "needs_auth": True, "auth_url": s["auth_url"]}
    if s.get("identity_error"):
        # Answer without tools, but say so — silently degrading is what made a
        # missing IAM permission look like the model choosing not to help.
        return {"reply": _IDENTITY_ERROR.format(err=s["identity_error"]),
                "needs_auth": False, "identity_error": s["identity_error"]}
    return {"reply": str(s["agent"](message)), "needs_auth": False}


def run_chat(actor_id: str, message: str, email: str = "",
             mem_sid: str = "") -> str:
    """Back-compat: assistant's final text (or the consent prompt)."""
    return chat_result(actor_id, message, email, mem_sid)["reply"]


def reauth(actor_id: str, idp: str = "lark") -> dict:
    """Start a fresh 3LO flow for `idp` even when a token is already vaulted →
    {auth_url}. Authorization is per-IdP; only "lark" is wired up so far (add a
    module like lark_3lo for each new downstream system)."""
    if idp not in ("", "lark"):
        return {"reply": f"IdP 尚未接入：{idp}", "needs_auth": False}
    # Drop every cached session for this user so the next turn re-reads the vault.
    with _lock:
        for key in [k for k in _sessions if k.startswith(f"{actor_id}|")]:
            s = _sessions.pop(key, None)
            if s and s.get("mcp"):
                try:
                    s["mcp"].__exit__(None, None, None)
                except Exception:
                    pass
    kind, value = lark_3lo.get_user_lark_token(actor_id, force=True)
    if kind == "auth_url":
        return {"reply": _AUTH_PROMPT.format(url=value),
                "needs_auth": True, "auth_url": value}
    return {"reply": "Already authorized.", "needs_auth": False}


def stream_chat(actor_id: str, message: str, email: str = "") -> Iterator[str]:
    """Streaming chat for the WebSocket path. Yields text deltas."""
    import asyncio

    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id))
    if s.get("auth_url"):
        yield _AUTH_PROMPT.format(url=s["auth_url"])
        return
    agent = s["agent"]
    loop = asyncio.new_event_loop()
    try:
        agen = agent.stream_async(message)
        while True:
            try:
                event = loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
            # Strands emits {"data": "<text chunk>"} for streamed model text.
            if isinstance(event, dict) and "data" in event:
                yield event["data"]
    finally:
        loop.close()
