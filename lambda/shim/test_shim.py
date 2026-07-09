"""Unit tests for the Lark OAuth shim — translation correctness without live AWS/Lark."""

import base64
import io
import json
import urllib.error
from unittest import mock

import index


def _evt(method, path, *, body="", qs=None, headers=None, b64=False):
    if b64:
        body = base64.b64encode(body.encode()).decode()
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method, "path": path}},
        "body": body,
        "isBase64Encoded": b64,
        "queryStringParameters": qs or {},
        "headers": headers or {},
    }


def _fake_urlopen(status, payload):
    m = mock.MagicMock()
    m.status = status
    m.read.return_value = json.dumps(payload).encode()
    m.__enter__.return_value = m
    return m


def test_authorize_redirects_to_accounts_host():
    r = index.handler(_evt("GET", "/authorize", qs={
        "client_id": "cli_x", "redirect_uri": "https://cb", "scope": "drive:drive offline_access",
        "state": "st1"}), None)
    assert r["statusCode"] == 302
    loc = r["headers"]["Location"]
    assert loc.startswith("https://accounts.larksuite.com/open-apis/authen/v1/authorize?")
    assert "client_id=cli_x" in loc and "state=st1" in loc
    assert "response_type=code" in loc


def test_authorization_code_success_strips_code_wrapper():
    lark_resp = {"code": "0", "access_token": "uat", "refresh_token": "rt",
                 "expires_in": 7200, "refresh_token_expires_in": 604800,
                 "scope": "drive:drive", "token_type": "Bearer"}
    with mock.patch.object(index.urllib.request, "urlopen",
                           return_value=_fake_urlopen(200, lark_resp)):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=authorization_code&client_id=cli&client_secret=sec&code=abc"), None)
    assert r["statusCode"] == 200
    b = json.loads(r["body"])
    assert b["access_token"] == "uat" and b["refresh_token"] == "rt"
    assert b["token_type"] == "Bearer" and b["expires_in"] == 7200
    assert "code" not in b  # the Lark envelope field must be gone


def test_authorization_code_forwards_pkce_verifier():
    captured = {}

    def _cap(req, timeout):  # noqa: ANN001
        captured["body"] = json.loads(req.data.decode())
        return _fake_urlopen(200, {"code": "0", "access_token": "t", "token_type": "Bearer", "expires_in": 1})
    with mock.patch.object(index.urllib.request, "urlopen", side_effect=_cap):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=authorization_code&client_id=cli&client_secret=sec&code=abc&code_verifier=VERIF123"), None)
    assert r["statusCode"] == 200
    assert captured["body"]["code_verifier"] == "VERIF123"  # PKCE forwarded to Lark


def test_refresh_grant_sends_refresh_body():
    captured = {}

    def _cap(req, timeout):  # noqa: ANN001
        captured["body"] = json.loads(req.data.decode())
        return _fake_urlopen(200, {"code": "0", "access_token": "new", "token_type": "Bearer",
                                   "expires_in": 7200})
    with mock.patch.object(index.urllib.request, "urlopen", side_effect=_cap):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=refresh_token&client_id=cli&client_secret=sec&refresh_token=old"), None)
    assert r["statusCode"] == 200
    assert captured["body"] == {"grant_type": "refresh_token", "client_id": "cli",
                                "client_secret": "sec", "refresh_token": "old"}


def test_error_200_with_nonzero_code_becomes_4xx():
    lark_err = {"code": "20073", "error": "invalid_grant",
                "error_description": "refresh_token already used"}
    # Lark answered HTTP 200 but with an error code — shim must force non-2xx.
    with mock.patch.object(index.urllib.request, "urlopen",
                           return_value=_fake_urlopen(200, lark_err)):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=refresh_token&client_id=cli&client_secret=sec&refresh_token=used"), None)
    assert r["statusCode"] == 400
    b = json.loads(r["body"])
    assert b["error"] == "invalid_grant" and "already used" in b["error_description"]
    assert "access_token" not in b


def test_error_4xx_preserves_status():
    err = urllib.error.HTTPError(index._TOKEN_URL, 400, "Bad Request", {},
                                 io.BytesIO(json.dumps(
                                     {"code": "20002", "error": "invalid_client",
                                      "error_description": "bad secret"}).encode()))
    with mock.patch.object(index.urllib.request, "urlopen", side_effect=err):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=authorization_code&client_id=cli&client_secret=bad&code=x"), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "invalid_client"


def test_client_secret_basic_header():
    creds = base64.b64encode(b"cli:sec").decode()
    captured = {}

    def _cap(req, timeout):  # noqa: ANN001
        captured["body"] = json.loads(req.data.decode())
        return _fake_urlopen(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 1})
    with mock.patch.object(index.urllib.request, "urlopen", side_effect=_cap):
        r = index.handler(_evt("POST", "/token",
            body="grant_type=authorization_code&code=abc",
            headers={"authorization": f"Basic {creds}"}), None)
    assert r["statusCode"] == 200
    assert captured["body"]["client_id"] == "cli" and captured["body"]["client_secret"] == "sec"


def test_unsupported_grant():
    r = index.handler(_evt("POST", "/token",
        body="grant_type=password&client_id=c&client_secret=s"), None)
    assert r["statusCode"] == 400
    assert json.loads(r["body"])["error"] == "unsupported_grant_type"


def test_return_url_completes_auth():
    # userId arrives via customState echoed back as `state`, base64url-encoded
    # (AgentCore rejects ':' in state). /return decodes it back.
    import base64
    st = base64.urlsafe_b64encode(b"lark:ou_x").decode().rstrip("=")
    with mock.patch.object(index, "_agentcore") as ac:
        r = index.handler(_evt("GET", "/return",
                               qs={"session_id": "sess-uri", "state": st}), None)
    ac.complete_resource_token_auth.assert_called_once_with(
        sessionUri="sess-uri", userIdentifier={"userId": "lark:ou_x"})
    assert r["statusCode"] == 200 and "Authorized" in r["body"]


def test_return_url_missing_userid():
    r = index.handler(_evt("GET", "/return", qs={"session_id": "sess-uri"}), None)
    assert r["statusCode"] == 400 and "userId" in r["body"]
