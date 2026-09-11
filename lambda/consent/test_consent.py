"""Unit tests for the 3LO return endpoint — no live AWS/Lark needed."""

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


def test_return_url_delegates_completion_to_the_router():
    # userId arrives via customState echoed back as `state`, base64url-encoded
    # (AgentCore rejects ':' in state). /return decodes it back.
    #
    # This endpoint does not complete the consent itself: the session belongs to the
    # JWT-derived vault namespace, so it must be named by the user's signed token, and
    # only the router mints those.
    import base64
    st = base64.urlsafe_b64encode(b"lark:ou_x").decode().rstrip("=")
    payload = mock.Mock()
    payload.read.return_value = b'{"ok": true}'
    with mock.patch.object(index, "_ROUTER_FUNCTION", "router-fn"), \
         mock.patch.object(index, "_lambda") as lam:
        lam.invoke.return_value = {"Payload": payload}
        r = index.handler(_evt("GET", "/return",
                               qs={"session_id": "sess-uri", "state": st}), None)
    completes = [c for c in lam.invoke.call_args_list
                 if b"_complete_consent" in c.kwargs["Payload"]]
    assert len(completes) == 1
    sent = json.loads(completes[0].kwargs["Payload"])
    assert sent["actorId"] == "lark:ou_x" and sent["sessionUri"] == "sess-uri"
    assert completes[0].kwargs["InvocationType"] == "RequestResponse"
    assert r["statusCode"] == 200 and "Authorized" in r["body"]


def test_return_url_reports_a_failed_completion():
    """A failed completion must not render "Authorized" — the user would walk away
    thinking they had consented."""
    import base64
    st = base64.urlsafe_b64encode(b"lark:ou_x").decode().rstrip("=")
    payload = mock.Mock()
    payload.read.return_value = b'{"ok": false, "error": "AccessDeniedException: nope"}'
    with mock.patch.object(index, "_ROUTER_FUNCTION", "router-fn"), \
         mock.patch.object(index, "_lambda") as lam:
        lam.invoke.return_value = {"Payload": payload}
        r = index.handler(_evt("GET", "/return",
                               qs={"session_id": "sess-uri", "state": st}), None)
    assert r["statusCode"] == 502 and "AccessDeniedException" in r["body"]


def test_return_url_missing_userid():
    r = index.handler(_evt("GET", "/return", qs={"session_id": "sess-uri"}), None)
    assert r["statusCode"] == 400 and "userId" in r["body"]
