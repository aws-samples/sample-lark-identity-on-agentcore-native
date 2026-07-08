"""Probe MCP server — measures AgentCore session↔user↔microVM mapping.

Not a real tool server. Its whoami tool reports:
  - instance_id: random, fixed at process start. Same value => same microVM;
    a changed value => a different microVM served this call.
  - request count seen by THIS instance.
  - the Mcp-Session-Id the platform routed us with.
  - a digest of the inbound Authorization header (what the Gateway injected).

Call it as two distinct user identities, several times each, and watch instance_id:
  same user repeated  -> stable instance_id  => 1:1 session reuse
  user A vs user B    -> different instance_id => per-user isolation

Contract (AWS docs): AgentCore MCP runtime expects a stateless streamable-HTTP
server on 0.0.0.0:8000/mcp; the platform injects Mcp-Session-Id for routing.
"""

import hashlib
import os
import secrets
import threading

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request

_INSTANCE_ID = secrets.token_hex(4)  # unique per process/microVM
_PID = os.getpid()
_lock = threading.Lock()
_count = 0

mcp = FastMCP("probe", host="0.0.0.0", port=8000, stateless_http=True)


def _digest(value: str) -> str:
    if not value:
        return "<none>"
    scheme, _, rest = value.partition(" ")
    return f"{scheme} …{hashlib.sha256(rest.encode()).hexdigest()[:8]} (len={len(rest)})"


@mcp.tool()
def whoami() -> str:
    """Report which microVM instance served this call, plus session/auth context."""
    global _count
    with _lock:
        _count += 1
        n = _count
    # FastMCP exposes the raw request via the request context.
    try:
        req: Request = mcp.get_context().request_context.request
        headers = req.headers
        session = headers.get("mcp-session-id", "<none>")
        auth = _digest(headers.get("authorization", ""))
    except Exception as e:  # noqa: BLE001
        session, auth = f"<unavailable: {e}>", "<unavailable>"
    return (
        f"instance_id={_INSTANCE_ID} pid={_PID} calls_seen_by_this_instance={n} "
        f"mcp_session_id={session} authorization={auth}"
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
