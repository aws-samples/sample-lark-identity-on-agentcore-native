"""Web search, via the AgentCore Gateway's built-in connector.

Unlike the Lark tools, search needs no end-user identity: it asks Amazon's web
index, not the user's account. So it goes through the Gateway with
GATEWAY_IAM_ROLE outbound auth and needs none of the per-user 3LO machinery the
Lark tools carry (see docs/agentcore-behavior.md).

The connector is only offered in us-east-1, so this gateway typically lives in a
different region from everything else and is called cross-region.

Inbound auth is a Cognito access token (not an ID token — the Gateway validates
the `client_id` claim, which only access tokens carry).
"""

from __future__ import annotations

import logging
import os

from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

import identity

log = logging.getLogger("agent.websearch")

_GATEWAY_URL = os.environ.get("WEBSEARCH_GATEWAY_URL", "")
_MCP_VERSION = "2025-11-25"  # the version the gateway was created with


def available() -> bool:
    return bool(_GATEWAY_URL)


def client_for(actor_id: str) -> MCPClient:
    """MCP client for the search gateway, authenticated as this user.

    The token is per-user even though search isn't: the Gateway authorises the
    caller, and reusing the same identity keeps the audit trail consistent.
    """
    token = identity.get_user_jwt(actor_id)
    return MCPClient(lambda: streamablehttp_client(
        _GATEWAY_URL,
        headers={
            "Authorization": f"Bearer {token}",
            # The Gateway is stateless and returns no session id; without this
            # header it negotiates 2025-03-26 and rejects the request (-32022).
            "MCP-Protocol-Version": _MCP_VERSION,
        },
    ))
