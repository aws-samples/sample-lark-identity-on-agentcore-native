"""E2E: presigned WSS bridge to the agent's port 18789.

This is the ONE unvalidated assumption in the design — that the AgentCore
platform auto-bridges a browser's presigned WSS connection to the agent's
WebSocket port (18789), for our from-scratch agent (not OpenClaw).

Skipped unless WS_URL is set (obtain from POST /api/session).
Run: WS_URL=... uv run --with websockets --with pytest python -m pytest tests/test_wss_bridge.py -v
"""

import asyncio
import json
import os

import pytest

WS_URL = os.environ.get("WS_URL")
pytestmark = pytest.mark.skipif(not WS_URL, reason="set WS_URL from POST /api/session")


async def _chat_once(ws_url: str, actor_id: str, message: str) -> str:
    import websockets  # imported lazily so the file collects without the dep
    collected = []
    async with websockets.connect(ws_url, max_size=64 * 1024) as ws:  # AgentCore WS frame limit is 64 KB
        await ws.send(json.dumps({"type": "chat", "actorId": actor_id, "message": message}))
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if frame["type"] == "delta":
                collected.append(frame["text"])
            elif frame["type"] == "final":
                break
            elif frame["type"] == "error":
                raise AssertionError(f"agent error: {frame['message']}")
    return "".join(collected)


def test_wss_bridge_streams_reply():
    actor = os.environ.get("TEST_ACTOR_ID", "lark:ou_test")
    reply = asyncio.run(_chat_once(WS_URL, actor, "Say the single word: pong"))
    assert reply.strip(), "expected a non-empty streamed reply"
    print("\nagent replied:", reply)
