"""Demo MCP tool targets behind the Gateway.

Two tools, both proving how the end-user identity injected by the Request
Interceptor flows to the tool:

- `whoami`        — reports the injected end-user id / tenant / that a credential
                    was injected (identity pass-through proof).
- `list_my_docs`  — acts AS the end-user: loads that user's Lark user_access_token
                    from Secrets Manager and calls the Lark drive API, so the
                    result is scoped to what THAT user can see in Lark
                    (permission inheritance). The agent never holds the token.

Identity arrives in the tools/call arguments as `_endUserId` (= `lark:{open_id}`),
injected by the interceptor (Lambda targets drop custom headers, so the
interceptor puts identity in the args).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.error

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_REGION = os.environ.get("AWS_REGION", "us-west-2")
_PREFIX = os.environ.get("RESOURCE_PREFIX", "lark-agent")
_API_DOMAIN = os.environ.get("LARK_API_DOMAIN", "https://open.larksuite.com").rstrip("/")
_LARK_SECRET_ID = os.environ.get("LARK_SECRET_ID", "")

_secrets = boto3.client("secretsmanager", region_name=_REGION)


def _tool_name(context) -> str:
    try:
        cc = context.client_context
        if cc and cc.custom:
            return cc.custom.get("bedrockAgentCoreToolName", "") or cc.custom.get("toolName", "")
    except Exception:
        pass
    return ""


def _identity(event: dict, cc_custom: dict) -> dict:
    propagated = {k.lower(): v for k, v in
                  (cc_custom.get("bedrockAgentCorePropagatedHeaders") or {}).items()}
    return {
        "endUserId": event.get("_endUserId", "unknown"),      # lark:{open_id}
        "endUserTenant": event.get("_endUserTenant", "unknown"),
        "authorizationReceived": bool(propagated.get("authorization")),
    }


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ------------------------------- whoami -------------------------------------

def _whoami(ident: dict) -> dict:
    return _result(
        f"whoami: you are '{ident['endUserId']}' (tenant '{ident['endUserTenant']}'). "
        f"The gateway injected a downstream credential: {ident['authorizationReceived']}. "
        f"The agent itself never saw this credential."
    )


# ------------------------------- list_my_docs -------------------------------

def _lark_creds() -> dict:
    return json.loads(_secrets.get_secret_value(SecretId=_LARK_SECRET_ID)["SecretString"])


def _load_user_token(open_id: str) -> dict | None:
    try:
        raw = _secrets.get_secret_value(SecretId=f"{_PREFIX}/user-tokens/{open_id}")["SecretString"]
        return json.loads(raw)
    except _secrets.exceptions.ResourceNotFoundException:
        return None


def _refresh_user_token(open_id: str, bundle: dict) -> dict | None:
    """Refresh an expired user_access_token via the single-use refresh_token."""
    if not bundle.get("refresh_token"):
        return None
    c = _lark_creds()
    body = json.dumps({
        "grant_type": "refresh_token", "client_id": c["appId"],
        "client_secret": c["appSecret"], "refresh_token": bundle["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        f"{_API_DOMAIN}/open-apis/authen/v2/oauth/token",
        data=body, method="POST", headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        logger.error("token refresh failed: %s", e)
        return None
    if not resp.get("access_token"):
        logger.error("token refresh error: %s", resp)
        return None
    now = int(time.time())
    new_bundle = {
        "user_access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token", bundle["refresh_token"]),
        "expires_at": now + int(resp.get("expires_in", 7200)),
        "refresh_expires_at": now + int(resp.get("refresh_token_expires_in", 0) or 0),
        "scope": resp.get("scope", bundle.get("scope", "")),
    }
    try:
        _secrets.put_secret_value(SecretId=f"{_PREFIX}/user-tokens/{open_id}",
                                  SecretString=json.dumps(new_bundle))
    except Exception as e:  # noqa: BLE001
        logger.error("failed to persist refreshed token: %s", e)
    return new_bundle


def _valid_user_token(open_id: str) -> str | None:
    bundle = _load_user_token(open_id)
    if not bundle:
        return None
    if time.time() >= bundle.get("expires_at", 0) - 60:
        bundle = _refresh_user_token(open_id, bundle) or None
        if not bundle:
            return None
    return bundle.get("user_access_token")


def _resolve_token(ident: dict):
    """Return (open_id, user_access_token) for the caller, or (None, err_result)."""
    actor = ident["endUserId"]  # lark:{open_id}
    if not actor.startswith("lark:"):
        return None, _result(f"cannot resolve a Lark user from identity '{actor}'", is_error=True)
    open_id = actor.split(":", 1)[1]
    token = _valid_user_token(open_id)
    if not token:
        return None, _result(
            "You haven't authorized document access yet (no user token on file). "
            "Open the web app in Lark and grant access, then try again."
        )
    return (open_id, token), None


def _lark(method: str, path: str, token: str, body: dict | None = None) -> tuple[dict | None, dict | None]:
    """Call a Lark REST endpoint as the user. Returns (data, err_result)."""
    url = f"{_API_DOMAIN}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, _result(f"Lark API error {e.code}: {e.read().decode()[:200]}", is_error=True)
    if resp.get("code") != 0:
        return None, _result(f"Lark API code {resp.get('code')}: {resp.get('msg')}", is_error=True)
    return resp.get("data", {}), None


def _list_my_docs(ident: dict, ev: dict) -> dict:
    (ctx, err) = _resolve_token(ident)
    if err:
        return err
    _, token = ctx
    # Lark's list-files API returns ONE folder level (not recursive). Pass
    # folder_token to descend into a subfolder; omit it for the drive root.
    folder = (ev.get("folder_token") or "").strip()
    q = "/open-apis/drive/v1/files?page_size=50"
    if folder:
        q += f"&folder_token={folder}"
    data, err = _lark("GET", q, token)
    if err:
        return err
    files = data.get("files", [])
    actor = ident["endUserId"]
    where = f"folder {folder}" if folder else "their Lark drive root"
    if not files:
        return _result(f"{actor} has no files in {where}.")
    # Surface each folder's token so the agent can drill into it on a follow-up call.
    lines = []
    for f in files[:50]:
        tok = f.get("token", "")
        if f.get("type") == "folder":
            lines.append(f"- 📁 {f.get('name')} (folder, folder_token={tok}) — {f.get('url')}")
        else:
            lines.append(f"- {f.get('name')} ({f.get('type')}, token={tok}) — {f.get('url')}")
    return _result(
        f"Contents of {where} that {actor} can access (this level only, {len(files)} "
        f"shown; call again with a folder_token to go deeper):\n" + "\n".join(lines)
    )


def _create_doc(ident: dict, ev: dict) -> dict:
    (ctx, err) = _resolve_token(ident)
    if err:
        return err
    _, token = ctx
    title = (ev.get("title") or "Untitled").strip()
    data, err = _lark("POST", "/open-apis/docx/v1/documents", token, {"title": title})
    if err:
        return err
    doc = data.get("document", {})
    doc_id = doc.get("document_id", "")
    content = (ev.get("content") or "").strip()
    if content and doc_id:
        # Append one text block at the document root (block_id == document_id).
        block = {"children": [{"block_type": 2, "text": {"elements": [
            {"text_run": {"content": content}}]}}], "index": 0}
        _, berr = _lark("POST", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                        token, block)
        if berr:
            return _result(f"Created doc '{title}' (id {doc_id}) but failed to add content: "
                           f"{berr['content'][0]['text']}", is_error=True)
    return _result(f"Created Lark doc '{title}' (document_id: {doc_id}). It belongs to you "
                   f"in your Lark space.")


def _edit_doc(ident: dict, ev: dict) -> dict:
    (ctx, err) = _resolve_token(ident)
    if err:
        return err
    _, token = ctx
    doc_id = (ev.get("document_id") or "").strip()
    content = (ev.get("content") or "").strip()
    if not doc_id or not content:
        return _result("edit_doc needs 'document_id' and 'content'.", is_error=True)
    block = {"children": [{"block_type": 2, "text": {"elements": [
        {"text_run": {"content": content}}]}}], "index": 0}
    _, err = _lark("POST", f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                   token, block)
    if err:
        return err
    return _result(f"Appended content to doc {doc_id} (as you, subject to your Lark edit rights).")


def _delete_doc(ident: dict, ev: dict) -> dict:
    (ctx, err) = _resolve_token(ident)
    if err:
        return err
    _, token = ctx
    file_token = (ev.get("document_id") or ev.get("file_token") or "").strip()
    ftype = (ev.get("type") or "docx").strip()
    if not file_token:
        return _result("delete_doc needs 'document_id' (the file token).", is_error=True)
    _, err = _lark("DELETE", f"/open-apis/drive/v1/files/{file_token}?type={ftype}", token)
    if err:
        return err
    return _result(f"Deleted {ftype} {file_token} (moved to your Lark trash). Only works if "
                   f"you own it.")


# ------------------------------- dispatch -----------------------------------

_TOOLS = {
    "whoami": lambda ident, ev: _whoami(ident),
    "list_my_docs": _list_my_docs,
    "create_doc": _create_doc,
    "edit_doc": _edit_doc,
    "delete_doc": _delete_doc,
}


def handler(event, context):
    cc_custom = {}
    try:
        if context.client_context and context.client_context.custom:
            cc_custom = dict(context.client_context.custom)
    except Exception:
        pass
    name = _tool_name(context)
    logger.info("tool invoked: name=%s event=%s", name, json.dumps(event)[:300])

    ev = event if isinstance(event, dict) else {}
    ident = _identity(ev, cc_custom)

    # tool name arrives gateway-prefixed, e.g. "demo-whoami___whoami"
    short = name.split("___")[-1] if name else ""
    fn = _TOOLS.get(short)
    if not fn:
        return _result(f"unknown tool: {name}", is_error=True)
    return fn(ident, ev)
