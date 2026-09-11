"""Agent-side per-user 3LO for Lark + direct connection to the lark-mcp Runtime.

Why this exists: the agent drives 3LO itself against AgentCore Identity and delivers
the user's vaulted Lark token to the lark-mcp Runtime in a custom passthrough header.

Required by the topology, not a workaround for a missing feature. The Gateway does do
per-user 3LO — verified @2026-08-19 against an OpenAPI target — but it cannot deliver
the token to an MCP server hosted on AgentCore Runtime: that endpoint authenticates
the transport with SigV4 and owns the Authorization header, so there is no slot left
for a per-user Bearer. Hence the custom passthrough header here. Moving the MCP server
to an addressable HTTPS endpoint is what would make the managed path applicable; see
docs/agentcore-behavior.md.

Flow per user (actor_id = "lark:{open_id}"):
  1. get_user_lark_token(actor_id):
       GetWorkloadAccessTokenForUserId → GetResourceOauth2Token(USER_FEDERATION)
       - token vaulted  → return ("token", <lark_user_access_token>)
       - not yet        → return ("auth_url", <url to send to the user in chat>)
  2. with a token, mcp_client_for(token) opens an MCP client to the lark-mcp
     Runtime over SigV4, passing the token in the custom header the sidecar
     copies to Authorization for official lark-mcp.

Non-blocking: we never poll waiting for consent. First turn returns an auth_url
(the agent posts it to Lark chat and ends the turn); a later turn finds the
token vaulted and proceeds.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import httpx           # for direct Lark REST calls (_token_owner)
import httpx2          # mcp 2.x vendors this httpx fork; its client type-checks auth against it

from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from strands.tools.mcp.mcp_client import MCPClient

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_PROVIDER = os.environ.get("LARK_OAUTH_PROVIDER", "lark-agent-3lo")
_WORKLOAD = os.environ.get("AGENT_WORKLOAD_NAME", "lark-agent-wl")
_RETURN_URL = os.environ.get("CONSENT_RETURN_URL", "")  # bare, allowlisted on the workload
_LARK_MCP_URL = os.environ.get("LARK_MCP_URL", "")        # SigV4 Runtime MCP invocations URL
_SCOPES = os.environ.get("LARK_SCOPES", "drive:drive docx:document offline_access").split()
_CUSTOM_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Lark-Token"
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")

log = logging.getLogger("agent.lark_3lo")

_agentcore = boto3.client("bedrock-agentcore", region_name=_REGION)


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# Which tokens have been confirmed to belong to the actor they are vaulted under.
# Keyed by a digest, never the token itself, and bounded — this is a cache, and a
# token that is already known good does not need re-checking every turn.
_VERIFIED: dict[str, str] = {}
_VERIFIED_MAX = 256


def _token_owner(token: str) -> str:
    """The open_id this Lark token actually belongs to, or "" if it can't be read.
    Needs no extra scope — the lark-cli server reads the same endpoint for whoami."""
    try:
        r = httpx.get(f"{_API_DOMAIN}/open-apis/authen/v1/user_info",
                      headers={"Authorization": f"Bearer {token}"}, timeout=10)
        return ((r.json().get("data") or {}).get("open_id") or "")
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the token's owner: %s", e)
        return ""


def _belongs_to(token: str, actor_id: str) -> bool:
    """Whether this token is really the actor's own.

    Consent completion binds the vaulted token to whatever userId the return-url was
    told (`state`), NOT to the Lark account that actually signed in. So forwarding a
    consent link is enough to get someone else's token vaulted under your name, and
    every later turn would act as them. Checking at the point of use closes that
    regardless of how the token got there. Fails closed: an owner we cannot establish
    is not accepted."""
    digest = hashlib.sha256(token.encode()).hexdigest()[:32]
    if _VERIFIED.get(digest) == actor_id:
        return True
    owner = _token_owner(token)
    expected = actor_id.split(":", 1)[1] if ":" in actor_id else actor_id
    if not owner or owner != expected:
        log.error("vaulted token does not belong to %s (owner=%s) — refusing it",
                  actor_id, owner[:12] + "…" if owner else "unknown")
        return False
    if len(_VERIFIED) >= _VERIFIED_MAX:
        _VERIFIED.pop(next(iter(_VERIFIED)))
    _VERIFIED[digest] = actor_id
    return True


def get_user_lark_token(actor_id: str, force: bool = False,
                        workload_token: str = "") -> tuple[str, str]:
    """Return ("token", <lark token>) if vaulted, else ("auth_url", <url>).

    actor_id is "lark:{open_id}" — carried through as customState so the return-url can
    complete the consent. force=True always starts a fresh 3LO flow, ignoring any
    vaulted token.

    `workload_token` is the workload access token the Runtime delivered with this
    request, derived from the caller's verified JWT. Passing it is what makes the
    identity unforgeable: this code never gets to *name* a user, it can only use the one
    the platform already authenticated. Absent it (no CUSTOM_JWT inbound) we fall back to
    deriving one from actor_id, which is a trusted-caller assumption — and a different
    vault namespace, so the two are not interchangeable for an existing deployment.

    A vaulted token is used only after it is confirmed to be this actor's own; one
    that isn't gets discarded in favour of a fresh consent flow (see _belongs_to).
    """
    if workload_token:
        wat = workload_token
    else:
        log.warning("no platform workload token for %s — falling back to ForUserId",
                    actor_id)
        wat = _agentcore.get_workload_access_token_for_user_id(
            workloadName=_WORKLOAD, userId=actor_id
        )["workloadAccessToken"]
    kwargs = dict(
        workloadIdentityToken=wat,
        resourceCredentialProviderName=_PROVIDER,
        scopes=_SCOPES,
        oauth2Flow="USER_FEDERATION",
        customState=_b64url(actor_id),
        forceAuthentication=force,
    )
    if _RETURN_URL:
        kwargs["resourceOauth2ReturnUrl"] = _RETURN_URL
    resp = _agentcore.get_resource_oauth2_token(**kwargs)
    token = resp.get("accessToken")
    if token and _belongs_to(token, actor_id):
        return "token", token
    if token:
        # Someone else's grant is sitting under this actor. Re-consent is the only way
        # out: the wrong token stays in the vault, so without forcing a fresh flow the
        # next call would fetch it right back. Guarded against recursion by `force`.
        if not force:
            return get_user_lark_token(actor_id, force=True,
                                       workload_token=workload_token)
        log.error("fresh authorization still produced a token for another account")
    return "auth_url", resp["authorizationUrl"]


class _SigV4HTTPXAuth(httpx2.Auth):
    """Sign every request (incl. SSE polls) with SigV4 for bedrock-agentcore.

    Subclasses the httpx fork mcp 2.x vendors (httpx2), NOT the real httpx: the
    vendored client rejects an httpx.Auth with TypeError: Invalid "auth" argument.
    The auth_flow protocol is identical."""

    def __init__(self, creds, service: str, region: str):
        self._signer = SigV4Auth(creds, service, region)

    def auth_flow(self, request: httpx2.Request):
        headers = dict(request.headers)
        headers.pop("connection", None)  # or the server rejects on signature mismatch
        aws_req = AWSRequest(method=request.method, url=str(request.url),
                             data=request.content, headers=headers)
        self._signer.add_auth(aws_req)
        request.headers.update(dict(aws_req.headers))
        yield request


def mcp_client_for(lark_token: str, url: str = "") -> MCPClient:
    """MCP client to an MCP-server Runtime: SigV4 transport + the Lark token in the
    custom passthrough header. Defaults to the lark-cli server; pass `url` for
    another one. Same transport either way — what differs is which tools the server
    exposes and which identity each of them uses.

    mcp 2.x moved headers/auth/timeout onto an httpx client you supply. Built inside
    the factory, not outside: the transport closes its client on exit, and strands
    may re-open the transport, which must not reuse a closed client."""
    creds = boto3.Session(region_name=_REGION).get_credentials()
    auth = _SigV4HTTPXAuth(creds, "bedrock-agentcore", _REGION)
    headers = {_CUSTOM_HEADER: lark_token}
    return MCPClient(lambda: streamable_http_client(
        url or _LARK_MCP_URL,
        http_client=create_mcp_http_client(
            headers=headers, auth=auth,
            # 60 s per operation; reads get longer for SSE polls (the old transport's
            # separate sse_read_timeout defaulted to 300 s).
            timeout=httpx2.Timeout(60.0, read=300.0)),
    ))
