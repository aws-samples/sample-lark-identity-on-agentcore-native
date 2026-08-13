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
in a custom header (the lark-mcp sidecar copies it to Authorization). The token
gates tool *calls*, not the connection — lark-mcp lists its tools without one — so
an unauthorized user gets a working session and is only asked to consent if the
model actually reaches for a Lark tool. We drive 3LO from the agent because the
Gateway does not do per-user 3LO injection for a CustomOauth2 provider (AWS gap,
agentcore-samples#1424).

`chat_result` returns the final text; `chat_async` streams it into a Lark card.
"""

from __future__ import annotations

import hashlib
import os
import time
import logging
import threading

from strands import Agent
from strands.models import BedrockModel

import lark_3lo
import lark_notify
import websearch

log = logging.getLogger("agent.core")

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5")
_MEMORY_ID = os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID", "")
# Empty unless ./deploy.sh approval ran — the approval tools are opt-in.
_APPROVAL_MCP_URL = os.environ.get("APPROVAL_MCP_URL", "")
_SYSTEM = os.environ.get(
    "AGENT_SYSTEM_PROMPT",
    "You are a helpful assistant embedded in Lark. Be concise. "
    "Use the provided tools when they help answer the user.",
)
# Rebuild a cached session before its Cognito access token (~1h) expires.
_SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", "3000"))  # 50 min
# An unauthorized session is cached only briefly: it works, but the moment the user
# consents we want the next turn to pick up the token.
_UNAUTH_TTL = int(os.environ.get("UNAUTH_SESSION_TTL_SECONDS", "60"))

_model = BedrockModel(model_id=_MODEL_ID, streaming=True)

# session_id -> {agent, mcp, created}. One microVM ≈ one session, so this is tiny.
_sessions: dict[str, dict] = {}
_lock = threading.Lock()

# Background turns in flight. AgentCore may reclaim an idle container, which would
# kill them, so /ping reports HealthyBusy while this is non-zero.
_in_flight = 0
_in_flight_lock = threading.Lock()


def busy() -> bool:
    with _in_flight_lock:
        return _in_flight > 0


def _track(delta: int) -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight += delta


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
    """Build a session for this user. Agent-side 3LO: fetch the user's vaulted Lark
    token if there is one and open an MCP client to lark-mcp (SigV4 + token in the
    custom header), entered once and kept open for the session.

    The token gates tool *calls*, not the connection: lark-mcp answers initialize
    and tools/list without it and only rejects tools/call with "authorize first"
    (verified against the deployed Runtime — an empty header returns the full tool
    list). So an unauthorized user still gets a session with the Lark tools listed,
    and can chat freely; consent is asked for when a tool is actually reached,
    which is the point at which the user can see what it is for."""
    mcp = None
    tools = []
    auth_url = None
    identity_error = None
    try:
        kind, value = lark_3lo.get_user_lark_token(actor_id)
    except Exception as e:  # noqa: BLE001 — never crash the turn on identity hiccups
        log.exception("3LO token lookup failed for %s", actor_id)
        kind, value = "error", str(e)

    if kind == "auth_url":
        auth_url = value  # remembered for the tool-call path, not returned upfront
    elif kind not in ("token", "auth_url"):
        # Surface identity failures instead of running tool-less: an agent that
        # merely says "I have no tools" reads as model behaviour and hides the
        # real cause (a missing IAM permission, most likely).
        identity_error = value

    if identity_error is None:
        try:
            mcp = lark_3lo.mcp_client_for(value if kind == "token" else "")
            mcp.__enter__()
            tools = mcp.list_tools_sync()
        except Exception:  # noqa: BLE001 — chat without Lark tools beats no reply
            log.exception("lark-mcp unavailable for %s", actor_id)
            mcp = None

    # Approval tools, when that Runtime is deployed. Separate server because its
    # write operations run on the app's tenant token (Lark accepts no user token
    # there) and its decision limits have to be bound to specific tools — see
    # .dev/adr/0006. The user token is still passed: add_sign needs it.
    approval_mcp = None
    if _APPROVAL_MCP_URL:
        try:
            approval_mcp = lark_3lo.mcp_client_for(
                value if kind == "token" else "", url=_APPROVAL_MCP_URL)
            approval_mcp.__enter__()
            tools = tools + approval_mcp.list_tools_sync()
        except Exception:  # noqa: BLE001 — optional, like search
            log.exception("approval tools unavailable for %s", actor_id)
            approval_mcp = None

    # Search doesn't depend on the user's Lark grant, so add it even when the
    # Lark tools are unavailable — an unauthorised user can still ask questions.
    search_mcp = None
    if websearch.available():
        try:
            search_mcp = websearch.client_for(actor_id)
            search_mcp.__enter__()
            tools = tools + search_mcp.list_tools_sync()
        except Exception:  # noqa: BLE001 — search is optional, Lark tools are not
            log.exception("web search unavailable for %s", actor_id)
            search_mcp = None

    agent = Agent(
        model=_model, system_prompt=_SYSTEM, tools=tools,
        session_manager=_make_session_manager(actor_id, mem_sid),
    )
    return {"agent": agent, "mcp": mcp, "search_mcp": search_mcp,
            "approval_mcp": approval_mcp,
            "created": time.time(),
            "auth_url": auth_url, "identity_error": identity_error}


_AUTH_PROMPT = (
    "To do that I need access to your Lark account. Please authorize once here, "
    "then send your message again:\n{url}"
)

# lark-mcp's reply when a tools/call arrives without a user token. It shows up as a
# tool-result content block, not necessarily in the model's final reply — the model
# may paraphrase or translate. Scanning the tool history is what makes this robust.
_NEEDS_TOKEN_MARKER = "no user token (authorize first)"


def _hit_auth_wall(session: dict) -> bool:
    """True when this turn needs consent — either because the stream was aborted at a
    Lark tool call (the fast path, before the model can editorialise) or because a
    tool result carried lark-mcp's refusal (the fallback, e.g. an already-authorized
    session whose token went stale mid-turn)."""
    if session.pop("walled_tool", None):
        return True
    return _hit_auth_wall_from_tool_results(session)


def _hit_auth_wall_from_tool_results(session: dict) -> bool:
    """True if this turn produced a lark-mcp tool result asking the user to
    authorize. Reads the agent's most-recent messages rather than the final reply,
    because the model paraphrases errors — 'no user token is available' would slip
    through a string check on the reply, and did in end-to-end testing."""
    if not session.get("auth_url"):
        return False
    agent = session.get("agent")
    if agent is None:
        return False
    # Only the current turn matters. The model's text answer follows any tool use,
    # so it is always the last message; the tool result that produced it is just
    # before. Cap at 4 to leave headroom for parallel tool calls in one turn.
    for m in getattr(agent, "messages", [])[-4:]:
        for c in m.get("content", []) or []:
            tr = c.get("toolResult") if isinstance(c, dict) else None
            if not tr:
                continue
            for block in tr.get("content", []) or []:
                if isinstance(block, dict) and _NEEDS_TOKEN_MARKER in (block.get("text") or ""):
                    return True
    return False


_IDENTITY_ERROR = (
    "I can't reach your Lark account right now, so I have no Lark tools this turn "
    "(other questions still work).\nDetails: {err}"
)


def _get_session(actor_id: str, email: str, mem_sid: str, fresh: bool = False) -> dict:
    """Return the cached session for this user, rebuilding it if absent or near
    token expiry. A pending-authorization session (no token yet) is NOT cached —
    so the next turn re-checks the vault and picks up a freshly consented token."""
    cache_key = f"{actor_id}|{mem_sid}"
    with _lock:
        s = _sessions.get(cache_key)
        # `fresh` is for the consent-resume path: the user has just authorized, so a
        # cached unauthorized session must not be reused — its MCP clients hold an
        # empty token and the turn would wall again. Measured: consent completed 31 s
        # after the prompt, well inside _UNAUTH_TTL, so waiting for expiry is not an
        # option.
        if s and fresh:
            for key in ("mcp", "search_mcp", "approval_mcp"):
                if s.get(key):
                    try:
                        s[key].__exit__(None, None, None)
                    except Exception:
                        pass
            _sessions.pop(cache_key, None)
            s = None
        if s:
            # An unauthorized session works (tools listed, calls rejected), so it is
            # worth caching — but only briefly, or the user consents and keeps being
            # told to authorize until the full TTL lapses.
            ttl = _UNAUTH_TTL if s.get("auth_url") else _SESSION_TTL
            if (time.time() - s["created"]) < ttl:
                return s
            for key in ("mcp", "search_mcp", "approval_mcp"):
                if s.get(key):
                    try:
                        s[key].__exit__(None, None, None)  # close the stale connection
                    except Exception:
                        pass
        s = _build_session(actor_id, email, mem_sid)
        if not s.get("identity_error"):
            _sessions[cache_key] = s
        return s


def chat_result(actor_id: str, message: str, email: str = "",
                mem_sid: str = "") -> dict:
    """Non-streaming chat → {reply, needs_auth, auth_url}. History via Memory.
    When the user hasn't authorized Lark yet, needs_auth is True and auth_url is
    the raw consent URL, so the caller (router) can drive the wait-for-consent
    loop instead of asking the user to re-send."""
    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id))
    if s.get("identity_error"):
        # Answer without tools, but say so — silently degrading is what made a
        # missing IAM permission look like the model choosing not to help.
        return {"reply": _IDENTITY_ERROR.format(err=s["identity_error"]),
                "needs_auth": False, "identity_error": s["identity_error"]}
    reply = str(s["agent"](message))
    if _hit_auth_wall(s):
        return {"reply": _AUTH_PROMPT.format(url=s["auth_url"]),
                "needs_auth": True, "auth_url": s["auth_url"]}
    return {"reply": reply, "needs_auth": False}


def run_chat(actor_id: str, message: str, email: str = "",
             mem_sid: str = "") -> str:
    """Back-compat: assistant's final text (or the consent prompt)."""
    return chat_result(actor_id, message, email, mem_sid)["reply"]


def chat_async(actor_id: str, message: str, chat_id: str, email: str = "",
               mem_sid: str = "", message_id: str = "", reaction_id: str = "",
               fresh_session: bool = False) -> dict:
    """Accept the work and answer later.

    A real task can outlast any request/response window (InvokeAgentRuntime caps at
    15 min, the calling Lambda at less), and a turn that times out mid-way is the
    worst outcome: the work often completed, but the user was told it failed. So we
    return an acknowledgement now and push the result to the chat when it's ready.

    An unauthorized user is not stopped here: the Lark tools are listed even without
    a token, so the turn runs and consent is only raised if the model actually calls
    one. By then the router has returned, so the prompt is pushed to the chat like
    any other answer and the user re-sends after approving."""
    s = _get_session(actor_id, email, mem_sid or _session_id_for(actor_id),
                     fresh=fresh_session)
    if s.get("identity_error"):
        return {"reply": _IDENTITY_ERROR.format(err=s["identity_error"]),
                "needs_auth": False, "identity_error": s["identity_error"]}

    def _run() -> None:
        try:
            _stream_to_chat(s, message, chat_id)
            if _hit_auth_wall(s):
                # A clickable "点击授权" link, matching the router's synchronous path,
                # instead of a raw URL. The message was parked before this turn, so
                # the shim's /return replays it once consent lands — no re-send.
                lark_notify.send_link(
                    chat_id, "需要访问你的 Lark 账号，授权后我会自动继续：",
                    "点击授权", s["auth_url"])
        except Exception as e:  # noqa: BLE001 — the caller is already gone
            log.exception("async turn failed for %s", actor_id)
            lark_notify.send_text(chat_id, f"Sorry, that didn't work out ({type(e).__name__}).")
        finally:
            # Clear the router's marker whatever happened — leaving it on a failed
            # turn would read as "still working". If the container dies outright
            # nobody clears it, which is cosmetic only.
            lark_notify.remove_reaction(message_id, reaction_id)
            _track(-1)

    _track(+1)
    threading.Thread(target=_run, name=f"turn-{actor_id[:16]}", daemon=True).start()
    return {"accepted": True, "needs_auth": False}


# No throttle here on purpose. The card write is queued, not performed — the card's
# own worker coalesces to the newest text and is paced by Lark's round trip (~470 ms
# measured), so handing over every delta costs a lock and sets no rate. The earlier
# throttle guarded a blocking write from this loop, and at 0.4 s it was tuned below the
# write's real cost: the loop lost over half its time to HTTP and the text jerked.


def _iter_deltas(agent, message: str, on_tool_use=None):
    """Yield text chunks from Strands' async stream on a private event loop.

    `on_tool_use(tool_name) -> bool` is consulted when the model starts a tool call;
    returning True abandons the stream. Used to cut a turn short the moment an
    unauthorized session reaches for a Lark tool."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        agen = agent.stream_async(message)
        while True:
            try:
                event = loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
            if isinstance(event, dict) and "data" in event:
                yield event["data"]
                continue
            # Strands emits no tool-*result* event (verified by logging every event
            # shape), only tool_use_stream with the tool being invoked. That is
            # enough: in an unauthorized session any Lark tool call is certain to be
            # refused, so seeing one start means the turn is already lost — stop here
            # rather than let the model narrate the refusal at length.
            if on_tool_use is None or not isinstance(event, dict):
                continue
            name = (event.get("current_tool_use") or {}).get("name", "")
            if name and on_tool_use(name):
                return
    finally:
        loop.close()


def _stream_to_chat(session: dict, message: str, chat_id: str) -> str:
    """Run the turn, typing the answer into a streaming card as it is produced, and
    return the full text. If the card can't be created or an update fails (e.g. the
    cardkit:card:write scope isn't granted), fall back to posting the final text —
    the answer must survive regardless. Consent replies are handled by the caller."""
    agent = session["agent"]
    card = lark_notify.StreamingCard(chat_id)
    streaming = card.open()

    # Unauthorized: the first Lark tool the model reaches for is certain to be
    # refused, and letting the turn run on means it narrates that refusal at length
    # before the consent card arrives. Stop at the tool call instead — the caller
    # sees `walled` and posts the card as the only reply.
    unauthorized = bool(session.get("auth_url"))

    def _abort_on_lark_tool(tool_name: str) -> bool:
        if not unauthorized or tool_name.startswith("WebSearch"):
            return False
        session["walled_tool"] = tool_name
        log.info("aborting turn: %s needs consent", tool_name)
        return True

    acc = []
    for delta in _iter_deltas(agent, message, on_tool_use=_abort_on_lark_tool):
        acc.append(delta)
        if streaming and not card.update("".join(acc)):
            streaming = False  # a failed write drops us to the fallback below
    text = "".join(acc)

    # Aborted for consent: whatever the model had started saying is a half-sentence
    # about a failure the user is about to be asked to fix, so replace it rather than
    # leave it on the card.
    if session.get("walled_tool"):
        text = "🔐 这一步需要访问你的 Lark 账号"

    if streaming:
        if not card.close(text):
            streaming = False
    if not streaming:
        # Either CardKit was never available or it failed mid-stream. Post the whole
        # answer as plain text so the user still gets it.
        if not lark_notify.send_text(chat_id, text):
            log.error("could not deliver reply to %s: %s", chat_id, text[:200])
    return text


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
