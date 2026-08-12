"""AgentCore container server: the HTTP contract on 8080.

  GET  /ping          -> {"status":"Healthy"}   (must respond within seconds)
  POST /invocations   -> action in {warmup, status, chat}
      chat       : {action,actorId,message,email?} -> {reply}  (history via Memory)
      chat_async : same + chatId -> {accepted:true}; result is pushed to the chat
                   (messageId/reactionId: the router's progress marker, cleared at the end)
      warmup : {action} -> {ready:true}
      status : {action} -> {ready, instance, uptime, sessionAge, + clock diagnostics}

Chat-only scope: the sibling interceptor variant also served a WebSocket path
for the Lark-embedded web UI; this variant's sole entrypoint is the Lark bot
webhook, so no WS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

_T_IMPORT_START = time.monotonic()

from aiohttp import web

import agent_core   # pulls in strands, boto3 and mcp — the bulk of start-up

_T_IMPORT_DONE = time.monotonic()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent.server")

# The earliest point this module can observe, so process age includes the imports
# above. monotonic, not time.time(): the wall clock inherits its base from the image
# the microVM is restored from, so time.time() at import can be minutes off —
# measured once at 777 s of "process age" inside a kernel up for 25 s.
_START = _T_IMPORT_START
# Identifies the process serving a request, generated per start-up. AgentCore has no
# instance-level API, so self-reporting is the only way to see turnover at all. It is
# per PROCESS, not per microVM. Note what it cannot do: prove isolation. Anything
# baked into the image or snapshot is copied to every restore — the kernel boot_id is
# identical across concurrent sessions for exactly that reason — so only post-start
# writes distinguish environments (verified separately: concurrent sessions cannot
# see each other's files).
_INSTANCE = uuid.uuid4().hex[:8]


def _kernel_uptime() -> float | None:
    """Seconds since the kernel booted, i.e. the microVM's own age — maintained by
    the kernel, not by this process. Comparing it with the process's `uptime` is what
    tells the two apart: process age can never exceed it, so a process claiming to be
    older than its kernel would mean the probe (or the guest clock) is wrong."""
    try:
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]), 1)
    except Exception:
        return None

# When each session was first seen here, so /status can report how long this compute
# has served this session — distinct from the microVM's own age (/proc/uptime) and
# from this process's age. Bounded: a long-lived process can see many sessions and
# this is only a diagnostic.
_SESSIONS_TRACKED = 256
_session_first_seen: dict[str, float] = {}
_HTTP_PORT = int(os.environ.get("PORT", "8080"))


# ----------------------------- HTTP contract --------------------------------

async def handle_ping(request: web.Request) -> web.Response:
    # HealthyBusy tells AgentCore a background turn is still running, so the
    # container isn't reclaimed out from under it.
    status = "HealthyBusy" if agent_core.busy() else "Healthy"
    return web.json_response({"status": status})


async def handle_invocations(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = payload.get("action", "chat")

    # AgentCore passes the runtime session id on every request (verified), which is
    # what lets this process report per-session age rather than only its own.
    sid = request.headers.get("x-amzn-bedrock-agentcore-runtime-session-id", "")
    now = time.monotonic()
    if sid:
        if sid not in _session_first_seen and len(_session_first_seen) >= _SESSIONS_TRACKED:
            _session_first_seen.pop(next(iter(_session_first_seen)))  # oldest inserted
        _session_first_seen.setdefault(sid, now)

    if action == "status":
        return web.json_response({
            "ready": True,
            "instance": _INSTANCE,
            # This process's age. Not the microVM's — a process starts after the
            # microVM does — and not "how long you have been served" either.
            "uptime": round(now - _START, 1),
            # The microVM's own age, from the kernel. This is the only field that
            # answers "how long has this compute been running".
            "kernelUptime": _kernel_uptime(),
            # How long THIS session has been served by THIS process, counted from its
            # first request. Independent of when the microVM booted.
            "sessionAge": round(now - _session_first_seen[sid], 1) if sid else None,
        })

    if action == "warmup":
        return web.json_response({"ready": True})

    if action == "reauth":
        actor_id = payload.get("actorId") or payload.get("userId") or "anonymous"
        idp = payload.get("message", "") or "lark"   # router sends the idp key here
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, agent_core.reauth, actor_id, idp
            )
            return web.json_response(result)
        except Exception as e:
            log.exception("reauth failed")
            return web.json_response({"error": str(e)})

    if action == "chat_async":
        actor_id = payload.get("actorId") or payload.get("userId") or "anonymous"
        message = payload.get("message", "")
        chat_id = payload.get("chatId", "")
        email = payload.get("email", "")
        mem_sid = payload.get("memorySessionId", "")
        # The router's in-progress reaction, for us to clear when the turn ends.
        msg_id = payload.get("messageId", "")
        reaction_id = payload.get("reactionId", "")
        # Set on the consent-resume replay: rebuild the session so the just-vaulted
        # token is picked up instead of the cached unauthorized one.
        fresh_session = bool(payload.get("freshSession"))
        if not (message and chat_id):
            return web.json_response({"error": "message and chatId required"}, status=400)
        try:
            # Returns as soon as the work is accepted; the result is pushed to the
            # chat later. Consent still comes back inline (see chat_async).
            result = await asyncio.get_event_loop().run_in_executor(
                None, agent_core.chat_async, actor_id, message, chat_id, email,
                mem_sid, msg_id, reaction_id, fresh_session,
            )
            return web.json_response(result)
        except Exception as e:
            log.exception("chat_async failed")
            return web.json_response({"error": str(e)})

    if action == "chat":
        actor_id = payload.get("actorId") or payload.get("userId") or "anonymous"
        message = payload.get("message", "")
        email = payload.get("email", "")
        # Memory thread id is chosen by the caller (router) — that's what makes
        # /reset and /new able to start a new thread without deleting anything.
        mem_sid = payload.get("memorySessionId", "")
        if not message:
            return web.json_response({"error": "message required"}, status=400)
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, agent_core.chat_result, actor_id, message, email, mem_sid
            )
            # {reply, needs_auth, auth_url?} — router drives the consent wait.
            return web.json_response(result)
        except Exception as e:
            log.exception("chat failed")
            # Return 200: AgentCore wraps non-2xx as RuntimeClientError and drops
            # the body, hiding the real error from callers. The Router surfaces
            # the error field instead.
            return web.json_response({"error": str(e)})

    return web.json_response({"error": f"unknown action: {action}"}, status=400)


# ------------------------------- bootstrap ----------------------------------

def build_http_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    app.router.add_post("/invocations", handle_invocations)
    return app


def _log_startup_breakdown() -> None:
    """Break down the cold start, since AgentCore documents none of it and X-Ray
    can't see it (its trace root is this process, so everything before OTel starts is
    invisible). Log-only on purpose: /status stays readable, and whoever is measuring
    cold starts is reading CloudWatch anyway.

    kernel age is the microVM's; the gap between it and this process's age is what
    AgentCore spent booting and pulling the image before we got control."""
    kernel = _kernel_uptime()
    proc = time.monotonic() - _START
    imports = _T_IMPORT_DONE - _T_IMPORT_START
    before_us = round(kernel - proc, 1) if kernel is not None else None
    log.info(
        "startup breakdown: microVM=%ss process=%.1fs imports=%.1fs before-our-code=%ss "
        "instance=%s", kernel, proc, imports, before_us, _INSTANCE,
    )


async def main() -> None:
    http_runner = web.AppRunner(build_http_app())
    await http_runner.setup()
    await web.TCPSite(http_runner, "0.0.0.0", _HTTP_PORT).start()
    log.info("HTTP contract on :%d", _HTTP_PORT)
    _log_startup_breakdown()

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
