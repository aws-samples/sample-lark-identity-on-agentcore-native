"""Unit tests for agent logic that doesn't require live AWS or the (ARM64) deps.

Run: cd agent && uv run --with boto3 --with pytest python -m pytest test_agent.py -v

Note: agent_core imports strands/mcp (installed as ARM64 wheels for the Lambda/
runtime target), which can't be imported on an x86 test host. We therefore test
its session-id logic by importing the function in isolation, and cover identity
(the security-critical part) directly.
"""

import base64
import hashlib
import json
import os
import re
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ------------------------------- identity -----------------------------------

def test_derive_password_deterministic_and_complex():
    import identity
    with mock.patch.object(identity, "_get_salt", return_value="test-salt"):
        p1 = identity._derive_password("lark:ou_abc")
        p2 = identity._derive_password("lark:ou_abc")
        p3 = identity._derive_password("lark:ou_xyz")
    assert p1 == p2                       # deterministic
    assert p1 != p3                       # per-user
    assert p1.endswith("Aa1!")            # complexity suffix
    assert len(p1) == 36


def test_jwt_exp_parses_unverified():
    import identity
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234567890}).encode()).decode().rstrip("=")
    token = f"header.{payload}.sig"
    assert identity._jwt_exp(token) == 1234567890.0


def test_jwt_exp_bad_token_returns_zero():
    import identity
    assert identity._jwt_exp("not-a-jwt") == 0.0


def test_ensure_user_email_sanitizes_colon():
    """open_id-based username 'lark:ou_x' must not produce an invalid email."""
    import identity
    captured = {}

    def fake_create(**kw):
        captured["email"] = next(a["Value"] for a in kw["UserAttributes"] if a["Name"] == "email")

    with mock.patch.object(identity, "_cognito") as c:
        from botocore.exceptions import ClientError
        c.admin_get_user.side_effect = ClientError(
            {"Error": {"Code": "UserNotFoundException"}}, "AdminGetUser")
        c.admin_create_user.side_effect = fake_create
        c.admin_set_user_password.return_value = {}
        with mock.patch.object(identity, "_get_salt", return_value="s"):
            identity._ensure_user("lark:ou_abc", "")
    assert ":" not in captured["email"]           # colon replaced
    assert captured["email"] == "lark-ou_abc@lark.local"


# ------------------------------- agent_core session id ----------------------

def test_session_id_deterministic_per_user():
    """Load just the _session_id_for function without importing the heavy deps."""
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py"), encoding="utf-8").read()
    ns = {"hashlib": hashlib}
    # exec only the function definition we care about
    start = src.index("def _session_id_for")
    end = src.index("\n\ndef ", start)
    exec(src[start:end], ns)
    sid = ns["_session_id_for"]
    assert sid("lark:ou_abc") == sid("lark:ou_abc")          # stable
    assert sid("lark:ou_abc") != sid("lark:ou_xyz")          # per-user
    assert sid("lark:ou_abc").startswith("sess-")


# ------------------------------- busy tracking ------------------------------
# /ping must report HealthyBusy while a background turn runs, or AgentCore
# reclaims the container and kills it. The counter is shared mutable state
# touched from two scopes, which is exactly where a missing `global` hides — an
# earlier version raised UnboundLocalError only at runtime, in production.

def _load_busy_helpers():
    """Load busy()/_track() alone: agent_core imports strands, which isn't
    installable on an x86 test host."""
    import threading
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py"), encoding="utf-8").read()
    ns = {"threading": threading, "_in_flight": 0, "_in_flight_lock": threading.Lock()}
    for name in ("def busy(", "def _track("):
        start = src.index(name)
        end = src.index("\n\ndef ", start)
        exec(src[start:end], ns)
    return ns


def test_busy_reflects_in_flight_turns():
    ns = _load_busy_helpers()
    busy, track = ns["busy"], ns["_track"]
    assert busy() is False
    track(+1)
    assert busy() is True
    track(+1)
    track(-1)
    assert busy() is True          # still one turn running
    track(-1)
    assert busy() is False         # idle again, so the container may be reclaimed


def test_track_is_usable_from_a_thread():
    """The decrement happens on the worker thread; a scoping bug shows up there."""
    import threading
    ns = _load_busy_helpers()
    busy, track = ns["busy"], ns["_track"]
    track(+1)
    err = []

    def worker():
        try:
            track(-1)
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not err, f"_track failed off the main thread: {err}"
    assert busy() is False


# ------------------------------- web search ---------------------------------
# Search is optional; with no gateway configured the agent must simply run
# without the tool rather than failing to build a session.

def test_websearch_gateway_contract():
    """websearch imports mcp (not installable here), so assert the two things that
    silently break the Gateway call: the protocol-version header, without which it
    negotiates 2025-03-26 and rejects with -32022, and an access token rather than
    an ID token, since the Gateway checks the client_id claim."""
    src = open(os.path.join(os.path.dirname(__file__), "websearch.py"), encoding="utf-8").read()
    assert "MCP-Protocol-Version" in src
    assert "2025-11-25" in src
    assert "get_user_jwt" in src        # identity.py returns the ACCESS token
    # available() must be a pure config check, so an unset gateway just means no tool
    assert 'os.environ.get("WEBSEARCH_GATEWAY_URL", "")' in src


# --------------------------- deferred authorization -------------------------
# A user without a vaulted Lark token still gets a working session: lark-mcp lists
# its tools without one and only rejects tools/call. Consent is raised when a tool
# is actually reached, so plain chat ("hello") is never gated on it.

def test_auth_marker_matches_the_mcp_server():
    """The client detects the auth wall by matching lark-mcp's rejection text. It is
    a cross-process string contract: if the server's wording changes and this does
    not, the user silently stops being offered a consent link."""
    here = os.path.dirname(__file__)
    core = open(os.path.join(here, "agent_core.py"), encoding="utf-8").read()
    server = open(os.path.join(here, "..", "mcp-server", "server.js"), encoding="utf-8").read()
    marker = re.search(r'_NEEDS_TOKEN_MARKER = "([^"]+)"', core).group(1)
    assert marker in server, (
        f"agent_core expects {marker!r} but mcp-server/server.js no longer says it"
    )


def test_hit_auth_wall_reads_tool_results_not_the_final_reply():
    """The check must inspect tool-result blocks, not the model's text answer: the
    model paraphrases errors, so 'no user token is available' in the reply slips
    past a string check on the reply — verified end-to-end before this fix."""
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py"), encoding="utf-8").read()
    ns = {}
    for name in ('_NEEDS_TOKEN_MARKER = ', 'def _hit_auth_wall('):
        start = src.index(name)
        end = src.index("\n\n\n", start)
        exec(src[start:end], ns)
    hit = ns["_hit_auth_wall"]

    class FakeAgent:
        def __init__(self, messages): self.messages = messages

    def sess(msgs): return {"auth_url": "https://consent", "agent": FakeAgent(msgs)}

    # 1. No agent → nothing to inspect, so no auth wall.
    assert hit({"auth_url": "https://consent"}) is False
    # 2. Text reply that paraphrases the error but no toolResult → NOT a wall.
    #    This is exactly the case that fooled the first version of this function.
    paraphrased = [{"role": "assistant", "content": [
        {"text": "It looks like no user token is available — please authorize"}]}]
    assert hit(sess(paraphrased)) is False
    # 3. Real toolResult carrying the marker → wall.
    real = [
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "1", "name": "lark_whoami"}}]},
        {"role": "user", "content": [{"toolResult": {"content": [
            {"text": "no user token (authorize first)"}]}}]},
    ]
    assert hit(sess(real)) is True
    # 4. Same tool history but the session is authorized → no wall (auth_url absent).
    assert hit({"agent": FakeAgent(real)}) is False


def test_unauthorized_session_still_connects_and_lists_tools():
    """The token is passed as an empty header rather than skipping the connection —
    verified against the deployed Runtime, which returns the full tool list for an
    empty token. Skipping it would leave the model unaware of its Lark tools."""
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py"), encoding="utf-8").read()
    build = src[src.index("def _build_session"):src.index("_AUTH_PROMPT =")]
    assert 'mcp_client_for(value if kind == "token" else "")' in build
    # auth_url must not short-circuit the connection any more.
    assert "elif kind ==" not in build, "auth_url should no longer skip the MCP client"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
