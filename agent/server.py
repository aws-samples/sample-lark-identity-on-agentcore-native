"""AgentCore container server: the HTTP contract on 8080.

  GET  /ping          -> {"status":"Healthy"}   (must respond within seconds)
  POST /invocations   -> action in {warmup, status, chat}
      chat       : {action,actorId,message,email?} -> {reply}  (history via Memory)
      chat_async : same + chatId -> {accepted:true}; result is pushed to the chat
      warmup : {action} -> {ready:true}
      status : {action} -> {ready, uptime}

Chat-only scope: the sibling interceptor variant also served a WebSocket path
for the Lark-embedded web UI; this variant's sole entrypoint is the Lark bot
webhook, so no WS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from aiohttp import web

import agent_core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent.server")

_START = time.time()
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

    if action == "status":
        return web.json_response({"ready": True, "uptime": round(time.time() - _START, 1)})

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
        if not (message and chat_id):
            return web.json_response({"error": "message and chatId required"}, status=400)
        try:
            # Returns as soon as the work is accepted; the result is pushed to the
            # chat later. Consent still comes back inline (see chat_async).
            result = await asyncio.get_event_loop().run_in_executor(
                None, agent_core.chat_async, actor_id, message, chat_id, email, mem_sid
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


async def main() -> None:
    http_runner = web.AppRunner(build_http_app())
    await http_runner.setup()
    await web.TCPSite(http_runner, "0.0.0.0", _HTTP_PORT).start()
    log.info("HTTP contract on :%d", _HTTP_PORT)

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
