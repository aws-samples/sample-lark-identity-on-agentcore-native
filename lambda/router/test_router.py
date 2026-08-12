"""Router unit tests — Lark webhook crypto + event handling.

Run: uv run --with cryptography --with boto3 python -m pytest lambda/router/test_router.py -v

Env is set before importing modules so boto3 clients construct without error;
AWS calls are mocked.
"""

import hashlib
import json
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("IDENTITY_TABLE_NAME", "test-identity")
os.environ.setdefault("LARK_SECRET_ID", "test/lark")
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x")

sys.path.insert(0, os.path.dirname(__file__))

ENCRYPT_KEY = "test-encrypt-key"

# Golden ciphertext: Lark's AES-256-CBC webhook framing (key=sha256(ENCRYPT_KEY),
# IV "0123456789abcdef" prepended, PKCS#7) of the payload below. Precomputed so the
# test never constructs a cipher itself — it exercises the production decrypt path
# against a fixed, real-format payload.
_GOLDEN_EVENT = {"type": "url_verification", "challenge": "abc123"}
_GOLDEN_CIPHERTEXT = (
    "MDEyMzQ1Njc4OWFiY2RlZjTCy6xCTjl4LV6d/+dDtlSdzPmqcTkTl1iyYaHzzRk+H4NOv2+gZhM3z9/EBitHGNigsYuEJZ1EgAioJmTx0ps="
)


@pytest.fixture
def lark_mod():
    import lark
    lark._creds_cache = {"appId": "cli_x", "appSecret": "s",
                         "verificationToken": "v", "encryptKey": ENCRYPT_KEY}
    lark._token_cache = {"token": "", "expires_at": 0.0}
    return lark


def test_decrypt_event_roundtrip(lark_mod):
    assert lark_mod.decrypt_event(_GOLDEN_CIPHERTEXT) == _GOLDEN_EVENT


def test_verify_signature_valid(lark_mod):
    import time
    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    nonce = "n1"
    sig = hashlib.sha256(f"{ts}{nonce}{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce,
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is True


def test_verify_signature_wrong_sig_fails(lark_mod):
    import time
    ts = str(int(time.time()))
    headers = {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": "deadbeef"}
    assert lark_mod.verify_signature(headers, b"body") is False


def test_verify_signature_replay_window(lark_mod):
    old_ts = "1000000000"  # year 2001 — well outside window
    body = b"body"
    sig = hashlib.sha256(f"{old_ts}n{ENCRYPT_KEY}".encode() + body).hexdigest()
    headers = {"X-Lark-Request-Timestamp": old_ts, "X-Lark-Request-Nonce": "n",
               "X-Lark-Signature": sig}
    assert lark_mod.verify_signature(headers, body) is False


def test_verify_signature_fail_closed_without_key(lark_mod):
    lark_mod._creds_cache = {"encryptKey": ""}
    assert lark_mod.verify_signature({"x-lark-signature": "x"}, b"b") is False


def test_send_message_chunks_long_text(lark_mod):
    calls = []

    class FakeResp:
        def __init__(self, d): self._d = d
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(req.data)
        return FakeResp({"code": 0})

    lark_mod._token_cache = {"token": "t", "expires_at": 9e18}
    with mock.patch.object(lark_mod.urllib.request, "urlopen", fake_urlopen):
        ok = lark_mod.send_message("oc_x", "A" * 45000)
    assert ok is True
    assert len(calls) == 3  # 45000 / 20000 -> 3 chunks


def test_challenge_regex_rejects_xss():
    import index
    assert index._CHALLENGE_RE.match("safe_challenge-1.2") is not None
    assert index._CHALLENGE_RE.match("<script>") is None


# --- IdP registry ---------------------------------------------------------
# A malformed registry must degrade to the single-provider defaults rather than
# leaving the bot with no way to authorize anyone.

def _load_idps_with(raw: str) -> dict:
    import index
    with mock.patch.dict(os.environ, {"IDP_REGISTRY": raw}):
        return index._load_idps()


def test_idp_registry_parsed():
    idps = _load_idps_with(json.dumps([
        {"key": "lark", "provider": "p-lark", "scopes": ["a"], "label": "Lark"},
        {"key": "google", "provider": "p-goog", "scopes": ["b"], "label": "Google"},
    ]))
    assert set(idps) == {"lark", "google"}
    assert idps["google"]["provider"] == "p-goog"


def test_idp_registry_falls_back_when_malformed():
    idps = _load_idps_with("{not json")
    assert list(idps) == ["lark"]          # still usable
    assert idps["lark"]["provider"]        # and points somewhere


def test_idp_registry_falls_back_when_empty():
    assert list(_load_idps_with("")) == ["lark"]


# --- Memory thread rotation -----------------------------------------------
# Resetting a conversation rotates the thread id instead of deleting events:
# instant, and the old events stay put. So a rotation must yield a NEW id, and
# an unrotated lookup must be stable.

def test_memory_session_is_stable_then_rotates():
    import identity
    stored = {}

    def fake_get_item(Key):
        item = stored.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def fake_put_item(Item):
        stored[(Item["PK"], Item["SK"])] = Item

    with mock.patch.object(identity._table, "get_item", side_effect=fake_get_item), \
         mock.patch.object(identity._table, "put_item", side_effect=fake_put_item):
        first = identity.get_or_create_memory_session("u1", "lark:ou_x")
        again = identity.get_or_create_memory_session("u1", "lark:ou_x")
        assert again == first                      # stable while not rotated
        rotated = identity.rotate_memory_session("u1")
        assert rotated != first
        assert identity.get_or_create_memory_session("u1", "lark:ou_x") == rotated


# --- Message counting -----------------------------------------------------
# Strands writes session/agent state events alongside the conversation, so a raw
# event count reads far higher than the number of messages (5 for one exchange).
# Only `conversational` payloads may be counted.

def _count_with(*pages):
    """Drive count_events over a sequence of {events, nextToken?} pages, and hand
    back the boto3 mock so callers can assert on the request itself."""
    import identity
    fake = mock.Mock()
    fake.list_events.side_effect = list(pages)
    with mock.patch.object(identity, "_memory_id", return_value="mem-1"), \
         mock.patch.object(identity.boto3, "client", return_value=fake):
        return identity.count_events("lark:ou_x", "sess-1"), fake


def _conv(role="USER"):
    return {"payload": [{"conversational": {"role": role}}]}


def test_count_events_ignores_state_events():
    events = [
        {"payload": [{"blob": '{"session_type": "AGENT"}'}]},
        {"payload": [{"blob": '{"agent_id": "default"}'}]},
        {"payload": [{"conversational": {"role": "USER"}}]},
        {"payload": [{"conversational": {"role": "ASSISTANT"}}]},
    ]
    (n, capped), _ = _count_with({"events": events})
    assert (n, capped) == (2, False)               # one exchange, not four


def test_count_events_filters_state_events_server_side():
    """maxResults is a scan window, not a result count: without this filter the
    page spends its budget on state events and stops early, which is how a
    70-message thread reported "67+"."""
    _, fake = _count_with({"events": [_conv()]})
    sent = fake.list_events.call_args.kwargs["filter"]
    assert sent["eventMetadata"][0]["left"]["metadataKey"] == "stateType"
    assert sent["eventMetadata"][0]["operator"] == "NOT_EXISTS"


def test_count_events_walks_pages_for_an_exact_number():
    """A nextToken is not "capped" — ordinary threads must report an exact count."""
    (n, capped), fake = _count_with(
        {"events": [_conv(), _conv()], "nextToken": "t1"},
        {"events": [_conv()]},                      # no token -> done
    )
    assert (n, capped) == (3, False)
    assert fake.list_events.call_count == 2
    assert fake.list_events.call_args.kwargs["nextToken"] == "t1"


def test_count_events_caps_runaway_threads():
    """Past the page budget it gives up and says so, rather than making unbounded
    calls for a diagnostic."""
    import identity
    pages = [{"events": [_conv()], "nextToken": f"t{i}"}
             for i in range(identity._COUNT_MAX_PAGES + 2)]
    (n, capped), fake = _count_with(*pages)
    assert capped is True
    assert n == identity._COUNT_MAX_PAGES
    assert fake.list_events.call_count == identity._COUNT_MAX_PAGES


def test_count_events_without_memory_is_zero():
    import identity
    with mock.patch.object(identity, "_memory_id", return_value=""):
        assert identity.count_events("lark:ou_x", "sess-1") == (0, False)


# --------------------------- microVM diagnostics ----------------------------
# /status exposes the compute layer for developers evaluating AgentCore. Two traps
# were hit getting here: reporting the process's own age (which the wall clock made
# nonsensical — 777 s inside a 25 s-old kernel), and mixing units so the two
# durations couldn't be compared.

def _microvm_line_with(response, session_id="ses_x"):
    import index
    with mock.patch.object(index, "invoke_agent", return_value=response) as inv:
        return index._microvm_line(session_id, "user_1", "lark:ou_x"), inv


def test_microvm_line_reports_kernel_age_and_session_age():
    import index
    line, inv = _microvm_line_with(
        {"instance": "abc12345", "kernelUptime": 1830.4, "sessionAge": 65.9, "uptime": 777.0})
    assert "abc12345" in line
    assert "1830 秒" in line and "65 秒" in line
    # The process's own uptime is untrustworthy (wall clock inherits the image's
    # base), so it must not appear.
    assert "777" not in line
    # Probing must not spend the whole Lambda budget on a cold start.
    assert inv.call_args.kwargs["action"] == "status"
    assert inv.call_args.kwargs["budget"] == index._PROBE_SECONDS


def test_microvm_line_uses_one_unit_so_the_two_are_comparable():
    """Both durations in seconds: the point is to subtract them (equal → this microVM
    started for you; age >> sessionAge → an existing one took over)."""
    line, _ = _microvm_line_with(
        {"instance": "abc12345", "kernelUptime": 7200, "sessionAge": 30})
    assert "7200 秒" in line and "30 秒" in line
    assert "分钟" not in line and "小时" not in line


def test_microvm_line_without_a_session_does_not_probe():
    """No session id means no microVM to ask about — and probing would create one."""
    import index
    with mock.patch.object(index, "invoke_agent") as inv:
        line = index._microvm_line("", "user_1", "lark:ou_x")
    assert inv.call_count == 0
    assert "尚未建立会话" in line


def test_microvm_line_retries_a_provisioning_conflict():
    """AgentCore returns a retryable 409 while it provisions a session. The chat path
    disables retries on purpose (a timeout must not replay a turn), so this path has
    to handle it itself or /status reports "unknown" during a cold start."""
    import index

    class RetryableConflictException(Exception):
        pass

    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RetryableConflictException("Session operation in progress")
        return {"instance": "abc12345", "kernelUptime": 30, "sessionAge": 5}

    with mock.patch.object(index, "invoke_agent", side_effect=flaky), \
         mock.patch.object(index.time, "sleep"):
        line = index._microvm_line("ses_x", "user_1", "lark:ou_x")
    assert len(calls) == 2
    assert "abc12345" in line


def test_microvm_line_gives_up_after_one_retry():
    """Bounded: /status must not stall on a session stuck mid-provision."""
    import index

    class RetryableConflictException(Exception):
        pass

    with mock.patch.object(index, "invoke_agent",
                           side_effect=RetryableConflictException("busy")) as inv, \
         mock.patch.object(index.time, "sleep"):
        line = index._microvm_line("ses_x", "user_1", "lark:ou_x")
    assert inv.call_count == 2
    assert "未知" in line


def test_microvm_line_survives_a_probe_failure():
    """Diagnostics must never break the command that carries them."""
    import index
    with mock.patch.object(index, "invoke_agent", side_effect=RuntimeError("timeout")):
        line = index._microvm_line("ses_x", "user_1", "lark:ou_x")
    assert "未知" in line


def test_microvm_line_tolerates_an_older_image():
    """A microVM running a previous image reports no instance field."""
    line, _ = _microvm_line_with({"reply": "pong"})
    assert "旧镜像" in line


def test_add_reaction_returns_id_and_never_raises():
    """The progress marker is best-effort: the reaction_id is needed to remove it
    later, but a failure must not stop the turn."""
    import lark
    ok = {"code": 0, "data": {"reaction_id": "RE_1"}}

    class FakeResp:
        def __init__(self, d): self._d = d
        def read(self): return json.dumps(self._d).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen", lambda *a, **k: FakeResp(ok)):
        assert lark.add_reaction("om_1") == "RE_1"

    # Lark rejecting it, and the call blowing up, both degrade to "" rather than raise.
    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen",
                           lambda *a, **k: FakeResp({"code": 99, "msg": "nope"})):
        assert lark.add_reaction("om_1") == ""
    with mock.patch.object(lark, "get_tenant_token", return_value="t"), \
         mock.patch.object(lark.urllib.request, "urlopen",
                           mock.Mock(side_effect=OSError("boom"))):
        assert lark.add_reaction("om_1") == ""
    # No message id (or no token) means nothing to react to.
    with mock.patch.object(lark, "get_tenant_token", return_value="t"):
        assert lark.add_reaction("") == ""


def test_status_shows_both_identities_and_masks_the_app_id():
    """/status must name both identities — the whole point of this sample is that
    tools act as the *user* while replies go out as the *app*. The appId is not a
    secret but is masked anyway: no reason to paste a full identifier into a chat."""
    import index
    with mock.patch.object(index.lark, "get_credentials",
                           return_value=("cli_a1b2c3d4e5f6g7h8", "s", "v", "k")):
        line = index._app_identity()
    assert line == "cli_a1b2…g7h8"
    assert "c3d4e5f6" not in line          # middle is not exposed

    # Unconfigured must not blow up or print an empty parenthesis.
    with mock.patch.object(index.lark, "get_credentials", return_value=("", "", "", "")):
        assert index._app_identity() == "未配置"


# --------------------------- consent resume ---------------------------------
# When a Lark tool hits an auth wall in the async run, the message is parked; the
# shim's /return replays it after consent. These pin the parts that must not drift:
# take is once-only, expiry is honoured, and resume actually re-invokes the agent.

def test_take_pending_auth_is_once_only_and_honours_ttl():
    import identity, time as _t
    store = {}
    def fake_get(Key): return {"Item": store[Key["SK"]]} if Key["SK"] in store else {}
    def fake_put(Item): store[Item["SK"]] = Item
    def fake_del(Key): store.pop(Key["SK"], None)
    with mock.patch.object(identity._table, "get_item", side_effect=fake_get), \
         mock.patch.object(identity._table, "put_item", side_effect=fake_put), \
         mock.patch.object(identity._table, "delete_item", side_effect=fake_del):
        identity.park_pending_auth("u1", "查待审批", "oc_1")
        first = identity.take_pending_auth("u1")
        assert first == {"message": "查待审批", "chatId": "oc_1"}
        # Second take returns nothing — replay must not happen twice.
        assert identity.take_pending_auth("u1") is None

        # An item past its ttl is treated as absent, even before DynamoDB sweeps it.
        store["PENDING_AUTH"] = {"message": "old", "chatId": "oc_1",
                                 "ttl": int(_t.time()) - 1}
        assert identity.take_pending_auth("u1") is None


def test_resume_replays_the_parked_message():
    import index, identity
    with mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "take_pending_auth",
                           return_value={"message": "查待审批", "chatId": "oc_1"}), \
         mock.patch.object(identity, "get_or_create_session", return_value="ses_x"), \
         mock.patch.object(identity, "get_or_create_memory_session", return_value="mem_x"), \
         mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("lark:ou_abc")
    assert inv.call_count == 1
    kw = inv.call_args.kwargs
    assert kw["action"] == "chat_async"
    assert kw["chat_id"] == "oc_1"
    assert inv.call_args.args[3] == "查待审批"   # message positional


def test_resume_is_a_noop_when_nothing_parked():
    """A user who ran /auth directly has no turn to resume — must not invoke."""
    import index, identity
    with mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(identity, "take_pending_auth", return_value=None), \
         mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("lark:ou_abc")
    assert inv.call_count == 0


def test_resume_ignores_malformed_actor():
    import index
    with mock.patch.object(index, "invoke_agent") as inv:
        index.resume_consented_turn("not-a-lark-id")
    assert inv.call_count == 0



# --------------------------- event-driven approval ---------------------------

def _approval_event(**over) -> dict:
    """A Lark `approval_task` event. Legacy 1.0 schema: no `header`, type inside
    `event`. Fields per the official doc, plus the `open_id` its sample payload shows
    but its field table omits."""
    ev = {"app_id": "cli_x", "type": "approval_task", "open_id": "ou_alice",
          "user_id": "b613t51g", "task_id": "t1", "instance_code": "i1",
          "approval_code": "AC1", "status": "PENDING", "operate_time": "1700000000000"}
    ev.update(over)
    return ev


def test_approval_event_dispatches_a_turn_addressed_to_the_approver():
    """An approval event carries no chat, so the approver's open_id is the delivery
    address — the senders read an `ou_` prefix as "DM this person"."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(index, "user_token_vaulted", return_value=True), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 1
    user_id, actor_id, message, chat_id = disp.call_args.args[:4]
    assert actor_id == "lark:ou_alice"
    assert chat_id == "ou_alice"
    # The ids are handed over rather than left to be discovered: an unattended turn has
    # nobody to ask, and user_id decides whose name the decision is recorded under.
    for token in ("i1", "t1", "AC1", "ou_alice"):
        assert token in message


def test_approval_event_ignores_a_settled_task():
    """The loop-breaker: the agent's own approve emits another event, and acting on it
    would decide the same instance again."""
    import index, identity
    for status in ("APPROVED", "REJECTED", "TRANSFERRED", "DONE"):
        with mock.patch.object(identity, "claim_approval_task", return_value=True), \
             mock.patch.object(index, "_dispatch_turn") as disp:
            index.process_approval_event(_approval_event(status=status))
        assert disp.call_count == 0, status


def test_approval_event_without_an_approver_is_skipped():
    """`approval_instance` events, and auto-approve tasks (documented as having an
    empty user), name nobody — there is no identity to act as."""
    import index, identity
    for missing in ({"open_id": ""}, {"task_id": ""}, {"instance_code": ""}):
        with mock.patch.object(identity, "claim_approval_task", return_value=True), \
             mock.patch.object(index, "_dispatch_turn") as disp:
            index.process_approval_event(_approval_event(**missing))
        assert disp.call_count == 0, missing


def test_approval_event_redelivery_decides_once():
    """Lark redelivers until acked, and the ack goes out long before the agent has
    decided. Without the claim the same task gets approved twice."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=False), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 0


def test_approval_event_for_an_unknown_user_stays_silent():
    """Someone outside the allowlist: their approval is their own business."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=(None, False)), \
         mock.patch.object(index, "_dispatch_turn") as disp:
        index.process_approval_event(_approval_event())
    assert disp.call_count == 0


def test_approval_event_parks_the_turn_when_the_approver_has_not_consented():
    """The approval server refuses to decide without that person's own grant, so the
    turn will wall. Parking is what lets consent-resume replay it after they authorize
    — otherwise an unattended turn is simply lost."""
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(index, "user_token_vaulted", return_value=False), \
         mock.patch.object(identity, "park_pending_auth") as park, \
         mock.patch.object(index, "_dispatch_turn"):
        index.process_approval_event(_approval_event())
    assert park.call_count == 1
    assert park.call_args.args[2] == "ou_alice"      # DM target, not a chat


def test_approval_event_does_not_park_for_an_authorized_approver():
    import index, identity
    with mock.patch.object(identity, "claim_approval_task", return_value=True), \
         mock.patch.object(identity, "resolve_user", return_value=("u1", False)), \
         mock.patch.object(index, "user_token_vaulted", return_value=True), \
         mock.patch.object(identity, "park_pending_auth") as park, \
         mock.patch.object(index, "_dispatch_turn"):
        index.process_approval_event(_approval_event())
    assert park.call_count == 0


def test_legacy_schema_event_is_routed_to_the_approval_path():
    """Message events put the type in `header` (schema 2.0); approval events put it in
    `event` (1.0). Reading only `header` drops every approval event on the floor."""
    import index
    body = json.dumps({"uuid": "u", "type": "event_callback",
                       "event": _approval_event()})
    with mock.patch.object(index, "process_approval_event") as appr:
        index.process_lark_event(body, {})
    assert appr.call_count == 1


def test_message_events_still_take_the_message_path():
    """The 2.0 header must keep winning — a regression here breaks the whole bot."""
    import index
    body = json.dumps({"header": {"event_type": "im.message.receive_v1"},
                       "event": {"sender": {"sender_type": "bot"}}})
    with mock.patch.object(index, "process_approval_event") as appr:
        index.process_lark_event(body, {})
    assert appr.call_count == 0


def test_dm_target_is_inferred_from_the_id_shape(lark_mod):
    """`ou_` is a person, anything else a chat. This is what lets the same senders
    serve an event-driven turn, which only ever knows the person."""
    assert lark_mod._receive_id_type("ou_alice") == "open_id"
    assert lark_mod._receive_id_type("oc_room") == "chat_id"
    assert lark_mod._receive_id_type("ou_alice", "chat_id") == "chat_id"   # explicit wins


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
