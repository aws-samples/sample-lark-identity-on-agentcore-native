"""Core agent: Strands Agent on Bedrock, with AgentCore Memory for session
continuity and an MCP Gateway client for per-user tool identity pass-through.

Server-reuse model (see AWS AgentCore + Strands guidance): the model, the MCP
client, and the agent are built ONCE per session and cached — not rebuilt per
message. Rebuilding per message re-handshakes the Gateway and re-lists tools
every time, which adds ~15–20s of latency. AgentCore gives each session its own
microVM, so the cache holds essentially one entry per container.

Memory: AgentCoreMemorySessionManager with batch_size=1 persists each turn to
Memory immediately (STM), so history survives idle-termination + a new microVM,
and is keyed by (actor_id, session_id) — one long thread per user, shared across
reconnects and both Lark entrypoints.

Identity pass-through: the user's Cognito access token is the Bearer on the MCP
connection, so the Gateway authorizer + interceptor see the real end-user; the
agent never holds a downstream tool credential. The token expires (~1h), so the
cached session is rebuilt after a TTL.

`run_chat` returns the final text; `stream_chat` yields text deltas.
"""

from __future__ import annotations

import hashlib
import os
import time
import logging
import threading
from datetime import timedelta
from typing import Iterator

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

from identity import get_user_jwt

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_GATEWAY_URL = os.environ.get("GATEWAY_URL", "").rstrip("/")
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


def _build_session(actor_id: str, email: str) -> dict:
    """Build a fresh (agent, mcp) for a session. MCP client is entered once and
    kept open; tools are listed once here, not per message."""
    session_id = _session_id_for(actor_id)
    mcp = None
    tools = []
    if _GATEWAY_URL:
        token = get_user_jwt(actor_id, email)
        mcp = MCPClient(lambda: streamablehttp_client(
            _GATEWAY_URL, headers={"Authorization": f"Bearer {token}"},
            timeout=timedelta(seconds=30),
        ))
        mcp.__enter__()  # persistent connection for the session's lifetime
        tools = mcp.list_tools_sync()
    agent = Agent(
        model=_model, system_prompt=_SYSTEM, tools=tools,
        session_manager=_make_session_manager(actor_id, session_id),
    )
    return {"agent": agent, "mcp": mcp, "created": time.time()}


def _get_session(actor_id: str, email: str) -> dict:
    """Return the cached session for this user, rebuilding it if absent or if its
    access token is near expiry."""
    session_id = _session_id_for(actor_id)
    with _lock:
        s = _sessions.get(session_id)
        if s and (time.time() - s["created"]) < _SESSION_TTL:
            return s
        if s and s.get("mcp"):
            try:
                s["mcp"].__exit__(None, None, None)  # close the stale connection
            except Exception:
                pass
        s = _build_session(actor_id, email)
        _sessions[session_id] = s
        return s


def run_chat(actor_id: str, message: str, email: str = "") -> str:
    """Non-streaming chat → assistant's final text. History via Memory."""
    agent = _get_session(actor_id, email)["agent"]
    return str(agent(message))


def stream_chat(actor_id: str, message: str, email: str = "") -> Iterator[str]:
    """Streaming chat for the WebSocket path. Yields text deltas."""
    import asyncio

    agent = _get_session(actor_id, email)["agent"]
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
