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
    src = open(os.path.join(os.path.dirname(__file__), "agent_core.py")).read()
    ns = {"hashlib": hashlib}
    # exec only the function definition we care about
    start = src.index("def _session_id_for")
    end = src.index("\n\ndef ", start)
    exec(src[start:end], ns)
    sid = ns["_session_id_for"]
    assert sid("lark:ou_abc") == sid("lark:ou_abc")          # stable
    assert sid("lark:ou_abc") != sid("lark:ou_xyz")          # per-user
    assert sid("lark:ou_abc").startswith("sess-")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
