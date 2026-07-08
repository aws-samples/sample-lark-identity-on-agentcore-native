"""Web API unit tests — presigned WSS + identity-from-claims correctness.

Run: uv run --with boto3 python -m pytest lambda/web_api/test_web_api.py -v
"""

import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("IDENTITY_TABLE_NAME", "t-identity")
os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
                      "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/abc")
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-west-2_pool")
os.environ.setdefault("COGNITO_CLIENT_ID", "client")
os.environ.setdefault("COGNITO_PASSWORD_SECRET_ID", "s/pw")
os.environ.setdefault("LARK_SECRET_ID", "s/lark")

sys.path.insert(0, os.path.dirname(__file__))


def test_presigned_ws_url_is_signed_wss():
    import index
    fake_creds = mock.Mock()
    fake_creds.get_frozen_credentials.return_value = type(
        "C", (), {"access_key": "AKIA", "secret_key": "secret", "token": None})()
    with mock.patch.object(index, "_session", mock.Mock(get_credentials=lambda: fake_creds)):
        url = index.generate_presigned_ws_url("ses_user_abc_0001", expires=300)
    assert url.startswith("wss://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/")
    assert "X-Amz-Signature=" in url
    assert "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=ses_user_abc_0001" in url
    # runtime ARN must be url-encoded into the path
    assert "arn%3Aaws%3Abedrock-agentcore" in url


def test_identity_from_claims_uses_username_not_sub():
    """The whole unified-identity design hinges on this: use cognito:username."""
    import index
    claims = {"sub": "11112222-3333-4444-5555-666677778888",
              "cognito:username": "lark:ou_abc123", "email": "u@co"}
    actor_id, open_id = index._identity_from_claims(claims)
    assert actor_id == "lark:ou_abc123"
    assert open_id == "ou_abc123"
    # must NOT be the random Cognito sub
    assert open_id != claims["sub"]


def test_claims_missing_returns_none():
    import index
    assert index._claims({"requestContext": {}}) is None


def test_create_session_rejects_unallowed_user():
    import index
    event = {"requestContext": {"authorizer": {"jwt": {"claims":
             {"cognito:username": "lark:ou_x", "email": ""}}}}}
    with mock.patch.object(index.identity, "resolve_user", return_value=(None, False)):
        resp = index.handle_create_session(event)
    assert resp["statusCode"] == 403


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
