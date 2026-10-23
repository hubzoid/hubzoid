"""Chat inside an app over AG-UI (hubzoid.embed.agui), on the scripted runtime."""
from __future__ import annotations

import time

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey

from tests.chat_helpers import build_app, events, make_hub

SECRET = "e" * 40
ORIGIN = "https://erp.example.com"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch, name="finance")
    monkeypatch.setenv("ERP_APP_SECRET", SECRET)
    (hub / "apps.yaml").write_text(f"""apps:
  erpnext:
    secret_ref: env:ERP_APP_SECRET
    origins: [{ORIGIN}]
    access: {{agents: [finance], tools: ["*"]}}
""")
    (hub / "hitl.yaml").write_text("version: 1\ntools:\n  reserve_stock: {ask: always}\n")
    from hubzoid.embed import agui, config

    config.reset()
    agui._runs.clear()      # the per-person limit is per minute, not per test
    agui._fails.clear()
    return hub


@pytest.fixture
def client(hub):
    from fastapi.testclient import TestClient

    with TestClient(build_app()) as c:
        yield c


def token(sub: str = "alice", **over) -> str:
    now = int(time.time())
    body = {"iss": "erpnext", "aud": "hubzoid", "sub": sub, "name": sub.title(), "iat": now, "exp": now + 600}
    body.update(over)
    return jwt.encode({"alg": "HS256"}, body, OctKey.import_key(SECRET.encode()))


def auth(sub: str = "alice", **over) -> dict:
    return {"Authorization": f"Bearer {token(sub, **over)}", "Origin": ORIGIN}


def run(client, text: str, *, thread: str = "t-1", sub: str = "alice", msg_id: str | None = None, **extra):
    body = {"threadId": thread, "runId": "run-" + str(time.time_ns()), "tools": [], "context": [],
            "messages": [{"id": msg_id or f"msg-{time.time_ns()}", "role": "user", "content": text}], **extra}
    resp = client.post("/api/agui", json=body, headers=auth(sub))
    return resp, ([e for e in events(resp.text) if isinstance(e, dict)] if resp.status_code == 200 else [])


def kinds(evs) -> list[str]:
    return [e["type"] for e in evs]


def text_of(evs) -> str:
    return "".join(e["delta"] for e in evs if e["type"] == "TEXT_MESSAGE_CONTENT")


def test_a_reply_streams_as_agui_events(client):
    resp, evs = run(client, "Hello there")
    assert resp.status_code == 200, resp.text
    assert resp.headers["access-control-allow-origin"] == ORIGIN
    k = kinds(evs)
    assert k[0] == "RUN_STARTED" and k[-1] == "RUN_FINISHED"
    assert {"TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"} <= set(k)
    assert "Hello there" in text_of(evs)
    assert evs[-1]["outcome"] == {"type": "success"} and evs[-1]["threadId"] == "t-1"


def test_tool_calls_show_as_agui_tool_events(client):
    _, evs = run(client, "use the tool please")
    k = kinds(evs)
    for name in ("TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"):
        assert name in k
    start = next(e for e in evs if e["type"] == "TOOL_CALL_START")
    assert start["toolCallName"] == "read_knowledge"
    # A call belongs to the text it follows, or to its own round of calls.
    texts = {e["messageId"] for e in evs if e["type"] == "TEXT_MESSAGE_START"}
    assert start["parentMessageId"] in texts or ".calls" in start["parentMessageId"]


def test_history_is_kept_per_person_and_never_shared(client):
    run(client, "First question", thread="shared-name")
    run(client, "Ben's question", thread="shared-name", sub="ben")
    alice = client.get("/api/agui/threads", headers=auth()).json()["threads"]
    ben = client.get("/api/agui/threads", headers=auth("ben")).json()["threads"]
    assert len(alice) == 1 and len(ben) == 1 and alice[0]["threadId"] != ben[0]["threadId"]
    loaded = client.get(f"/api/agui/threads/{alice[0]['threadId']}", headers=auth()).json()
    assert [m["role"] for m in loaded["messages"]] == ["user", "assistant"]
    assert loaded["messages"][0]["content"] == "First question"
    # Ben names Alice's conversation id: it maps into his own space, never hers.
    other = client.get(f"/api/agui/threads/{alice[0]['threadId']}", headers=auth("ben"))
    assert other.status_code == 404
    # The client's thread name keeps working for its owner, and reopening by id continues it.
    run(client, "Second question", thread=alice[0]["threadId"])
    again = client.get(f"/api/agui/threads/{alice[0]['threadId']}", headers=auth()).json()
    assert [m["content"] for m in again["messages"] if m["role"] == "user"] == ["First question", "Second question"]


def test_page_context_is_labelled_data_and_hidden_from_history(client):
    _, evs = run(client, "What is this?", forwardedProps={"hubzoid": {"page": {"doctype": "Sales Invoice",
                                                                               "name": "INV-1"}}})
    threads = client.get("/api/agui/threads", headers=auth()).json()["threads"]
    loaded = client.get(f"/api/agui/threads/{threads[0]['threadId']}", headers=auth()).json()
    assert loaded["messages"][0]["content"] == "What is this?"
    from hubzoid.chat.store import for_hub

    hub_dir = client.app.state.chat.hub_dir
    stored = for_hub(hub_dir).get_message(loaded["messages"][0]["id"])
    text = stored["content"][0]["text"]
    assert '<page-context app="erpnext"' in text and "not instructions" in text and "INV-1" in text
    # The agent reads it as data (the scripted runtime says what it was given).
    _, evs = run(client, "whatpage is this", forwardedProps={"hubzoid": {"page": {"doctype": "Sales Order",
                                                                                 "name": "SO-7"}}})
    assert "SO-7" in text_of(evs)


def test_an_approval_is_an_interrupt_and_resume_continues(client):
    _, evs = run(client, "please reserve stock", thread="orders")
    end = evs[-1]
    assert end["type"] == "RUN_FINISHED" and end["outcome"]["type"] == "interrupt"
    interrupt = end["outcome"]["interrupts"][0]
    assert interrupt["reason"] == "approval" and interrupt["metadata"]["hubzoid"]["tool"] == "reserve_stock"
    # Someone else can't answer it.
    other = client.post("/api/agui", headers=auth("ben"), json={
        "threadId": "orders", "runId": "r2", "messages": [], "tools": [], "context": [],
        "resume": [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": True}}]})
    assert other.status_code == 404
    resp = client.post("/api/agui", headers=auth(), json={
        "threadId": "orders", "runId": "r3", "messages": [], "tools": [], "context": [],
        "resume": [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": True}}]})
    assert resp.status_code == 200, resp.text
    cont = [e for e in events(resp.text) if isinstance(e, dict)]
    assert kinds(cont)[0] == "RUN_STARTED" and kinds(cont)[-1] == "RUN_FINISHED"
    from hubzoid.hitl import service

    hub_dir = client.app.state.chat.hub_dir
    req = service.get(hub_dir, client.app.state.chat.hub, interrupt["id"])
    assert req["status"] == "approved" and req["decided_via"] == "app"


def _resume(client, interrupt_id: str, entry: dict, *, thread: str = "orders"):
    return client.post("/api/agui", headers=auth(), json={
        "threadId": thread, "runId": "r-" + str(time.time_ns()), "messages": [], "tools": [], "context": [],
        "resume": [{"interruptId": interrupt_id, **entry}]})


def test_an_unclear_answer_to_an_approval_is_refused(client):
    from hubzoid.hitl import service

    _, evs = run(client, "please reserve stock", thread="orders")
    interrupt = evs[-1]["outcome"]["interrupts"][0]
    for entry in ({"status": "resolved", "payload": {}},
                  {"status": "resolved", "payload": "yes"},
                  {"status": "resolved", "payload": {"approved": "true"}},
                  {"status": "maybe", "payload": {"approved": True}}):
        assert _resume(client, interrupt["id"], entry).status_code == 422, entry
    state = client.app.state.chat
    assert service.get(state.hub_dir, state.hub, interrupt["id"])["status"] == "pending"


def test_a_saved_answer_whose_reply_did_not_continue_continues_on_retry(client):
    from hubzoid.hitl import service

    _, evs = run(client, "please reserve stock", thread="orders")
    interrupt = evs[-1]["outcome"]["interrupts"][0]
    state = client.app.state.chat
    req = service.get(state.hub_dir, state.hub, interrupt["id"])
    # Decided elsewhere with no fresh token: an app reply never continues on its own.
    service.decide(state.hub_dir, state.hub, req["id"], account_id=req["requester_id"],
                   email=req["requester_id"], decision="approve")
    assert service.get(state.hub_dir, state.hub, req["id"])["continued"] is False
    resp = _resume(client, interrupt["id"], {"status": "resolved", "payload": {"approved": True}})
    assert resp.status_code == 200, resp.text
    assert kinds([e for e in events(resp.text) if isinstance(e, dict)])[-1] == "RUN_FINISHED"
    assert service.get(state.hub_dir, state.hub, req["id"])["continued"] is True
    again = _resume(client, interrupt["id"], {"status": "resolved", "payload": {"approved": True}})
    assert again.status_code == 409


def test_an_answer_whose_reply_could_not_start_says_so(client, monkeypatch):
    from hubzoid.hitl import service

    _, evs = run(client, "please reserve stock", thread="orders")
    interrupt = evs[-1]["outcome"]["interrupts"][0]
    runs = client.app.state.chat.runs
    real = runs.start

    def fails(**kw):
        raise RuntimeError("could not start")

    monkeypatch.setattr(runs, "start", fails)
    first = _resume(client, interrupt["id"], {"status": "resolved", "payload": {"approved": True}})
    assert first.status_code == 200 and "could_not_start" in first.text
    state = client.app.state.chat
    assert service.get(state.hub_dir, state.hub, interrupt["id"])["status"] == "approved"
    monkeypatch.setattr(runs, "start", real)
    # The answer stands in the chat; the person carries on with a new message.
    assert _resume(client, interrupt["id"], {"status": "resolved", "payload": {"approved": True}}).status_code == 409
    resp, evs = run(client, "carry on please", thread="orders")
    assert resp.status_code == 200 and evs[-1]["type"] == "RUN_FINISHED"


def test_a_cancelled_question_is_cancelled_not_rejected(client):
    from hubzoid.hitl import service

    _, evs = run(client, "askme which warehouse", thread="q")
    interrupt = evs[-1]["outcome"]["interrupts"][0]
    blank = _resume(client, interrupt["id"], {"status": "resolved", "payload": {"answer": "  "}}, thread="q")
    assert blank.status_code == 422
    resp = _resume(client, interrupt["id"], {"status": "cancelled"}, thread="q")
    assert resp.status_code == 200, resp.text
    state = client.app.state.chat
    assert service.get(state.hub_dir, state.hub, interrupt["id"])["status"] == "cancelled"


@pytest.mark.parametrize("headers,code", [
    ({}, 401),
    ({"Authorization": "Bearer not-a-jwt"}, 401),
    ({"Authorization": f"Bearer {token(exp=int(time.time()) - 600, iat=int(time.time()) - 900)}"}, 401),
    ({"Authorization": f"Bearer {token(access={'agent': 'hr'})}"}, 403),
    ({"Authorization": f"Bearer {token()}", "Origin": "https://evil.example.com"}, 403),
])
def test_refused_requests(client, headers, code):
    resp = client.get("/api/agui/threads", headers=headers)
    assert resp.status_code == code, resp.text


def test_a_hubzoid_cookie_and_an_app_token_together_are_refused(client):
    client.cookies.set("hz_session", "x")
    try:
        assert client.get("/api/agui/threads", headers=auth()).status_code == 400
    finally:
        client.cookies.clear()


def test_cors_preflight_only_for_the_apps_origins(client):
    ok = client.options("/api/agui", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "authorization,content-type"})
    assert ok.status_code == 204 and ok.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in ok.headers
    bad = client.options("/api/agui", headers={"Origin": "https://evil.example.com",
                                                "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403
    # Other routes stay as they were: no CORS for the web app's own API.
    other = client.get("/api/conversations", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in other.headers


def test_the_agent_must_be_within_the_app(client, hub):
    (hub / "apps.yaml").write_text(f"""apps:
  erpnext:
    secret_ref: env:ERP_APP_SECRET
    origins: [{ORIGIN}]
    access: {{agents: [hr], tools: ["*"]}}
""")
    import os

    os.utime(hub / "apps.yaml", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    resp, _ = run(client, "hello")
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "no_access"


def test_app_runs_have_no_hubzoid_account(client):
    run(client, "Hello")
    from hubzoid.auth import users

    hub_dir = client.app.state.chat.hub_dir
    from hubzoid.embed.caller import subject

    assert users.store(hub_dir).find_by_email(subject("erpnext", "alice")) is None
    from hubzoid.access import run_identity

    import sqlalchemy as sa

    with run_identity._engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(run_identity.records).where(
            run_identity.records.c.started_from == "app")).all()
    assert rows and rows[-1]._mapping["acts_as"] is None
    assert rows[-1]._mapping["credential_kind"] == "app" and rows[-1]._mapping["credential_id"] == "erpnext"


APP_TOOL = '''"""app_lookup: calls the embedding app's API as the person (test fixture)."""
import hashlib

from agents import function_tool

from hubzoid.embed import app_token


@function_tool
def app_lookup(query: str) -> str:
    """Look up records in the app."""
    token = app_token()
    if token is None:
        return "no token"
    if query == "echo":
        return "raw " + token
    return "got " + hashlib.sha256(token.encode()).hexdigest()[:12]
'''


def _app_tool_hub(hub, tools: str, token_to: str):
    (hub / "tools_local").mkdir(exist_ok=True)
    (hub / "tools_local" / "app_lookup.py").write_text(APP_TOOL)
    (hub / "apps.yaml").write_text(f"""apps:
  erpnext:
    secret_ref: env:ERP_APP_SECRET
    origins: [{ORIGIN}]
    access: {{agents: [finance], tools: {tools}}}
    token_to: {token_to}
""")
    import os

    os.utime(hub / "apps.yaml", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))


def _app_token_run(client, **pass_claims):
    body = {"threadId": "tools-" + str(time.time_ns()), "runId": "r", "tools": [], "context": [],
            "messages": [{"id": f"msg-{time.time_ns()}", "role": "user", "content": "run the apptool"}]}
    resp = client.post("/api/agui", json=body, headers={"Authorization": f"Bearer {token(**pass_claims)}",
                                                        "Origin": ORIGIN})
    assert resp.status_code == 200, resp.text
    return text_of([e for e in events(resp.text) if isinstance(e, dict)])


def test_the_persons_app_token_reaches_only_the_named_tool(client, hub):
    import hashlib

    _app_tool_hub(hub, '["*"]', "[app_lookup]")
    said = _app_token_run(client, app_token="erp-secret-token-of-alice")
    assert "got " + hashlib.sha256(b"erp-secret-token-of-alice").hexdigest()[:12] in said
    _app_tool_hub(hub, '["*"]', "[]")
    assert "no token" in _app_token_run(client, app_token="erp-secret-token-of-alice")


def test_a_tool_outside_the_apps_limit_is_refused(client, hub):
    _app_tool_hub(hub, "[read_knowledge]", "[app_lookup]")
    said = _app_token_run(client, app_token="t-0123456789")
    assert "is not one of the tools this app allows" in said
    # The pass can narrow the limit further, never widen it.
    _app_tool_hub(hub, "[app_lookup, read_knowledge]", "[app_lookup]")
    said = _app_token_run(client, app_token="t-0123456789", access={"tools": ["read_knowledge"]})
    assert "is not one of the tools this app allows" in said


def _start_slow(client, thread: str):
    import threading

    box = {}

    def go():
        box["resp"] = client.post("/api/agui", headers=auth(), json={
            "threadId": thread, "runId": "slow", "tools": [], "context": [],
            "messages": [{"id": f"msg-{time.time_ns()}", "role": "user", "content": "go slow please"}]})

    t = threading.Thread(target=go)
    t.start()
    conv = None
    for _ in range(100):
        listed = client.get("/api/agui/threads", headers=auth()).json()["threads"]
        if listed:
            conv = listed[0]["threadId"]
            state = client.get(f"/api/agui/threads/{conv}", headers=auth()).json()
            if state["running"]:
                break
        time.sleep(0.05)
    return t, box, conv


def test_rejoin_stop_and_one_reply_at_a_time(client, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "4")
    t, box, conv = _start_slow(client, "slow-thread")
    assert conv is not None
    busy, _ = run(client, "another one", thread=conv)
    assert busy.status_code == 409
    other = client.get(f"/api/agui/threads/{conv}/run", headers=auth("ben"))
    assert other.status_code == 404          # not Ben's chat
    stopped = client.post(f"/api/agui/threads/{conv}/stop", headers=auth())
    assert stopped.status_code == 202 and stopped.json()["status"] == "stopping"
    t.join(timeout=30)
    first = [e for e in events(box["resp"].text) if isinstance(e, dict)]
    assert first[-1]["type"] == "RUN_FINISHED" and first[-1]["outcome"] == {"type": "cancelled"}
    # Nothing running now: rejoin answers 204.
    assert client.get(f"/api/agui/threads/{conv}/run", headers=auth()).status_code == 204


def test_rejoin_replays_the_running_reply(client, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "2")
    t, box, conv = _start_slow(client, "rejoin-thread")
    joined = client.get(f"/api/agui/threads/{conv}/run", headers=auth())
    t.join(timeout=30)
    assert joined.status_code == 200
    evs = [e for e in events(joined.text) if isinstance(e, dict)]
    assert kinds(evs)[0] == "RUN_STARTED" and kinds(evs)[-1] == "RUN_FINISHED"
    original = [e for e in events(box["resp"].text) if isinstance(e, dict)]
    assert text_of(evs) == text_of(original) and text_of(evs)


def test_turning_the_app_off_stops_its_replies(client, hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "8")
    t, box, conv = _start_slow(client, "off-thread")
    (hub / "apps.yaml").write_text(f"""apps:
  erpnext:
    enabled: false
    secret_ref: env:ERP_APP_SECRET
    origins: [{ORIGIN}]
    access: {{agents: [finance], tools: ["*"]}}
""")
    import os

    os.utime(hub / "apps.yaml", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    t.join(timeout=30)
    evs = [e for e in events(box["resp"].text) if isinstance(e, dict)]
    assert evs[-1]["outcome"] == {"type": "cancelled"}
    assert client.get("/api/agui/threads", headers=auth()).status_code == 403   # app_disabled


def test_deleting_a_chat(client):
    run(client, "keep this short", thread="to-delete")
    conv = client.get("/api/agui/threads", headers=auth()).json()["threads"][0]["threadId"]
    assert client.delete(f"/api/agui/threads/{conv}", headers=auth("ben")).status_code == 404
    assert client.delete(f"/api/agui/threads/{conv}", headers=auth()).status_code == 204
    assert client.get("/api/agui/threads", headers=auth()).json()["threads"] == []


def test_history_keeps_the_order_a_reply_was_written_in():
    from hubzoid.embed.agui import agui_messages

    path = [{"id": "u1", "role": "user", "content": [{"type": "text", "text": "hi\n\n<page-context app=\"x\">\n[]\n</page-context>"}]},
            {"id": "a1", "role": "assistant", "content": [
                {"type": "text", "text": "Looking. "},
                {"type": "tool-call", "toolCallId": "c1", "toolName": "t", "args": {"q": 1}, "result": {"status": "ok"}},
                {"type": "text", "text": "Done."}]}]
    out = agui_messages(path)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant"]
    assert out[0]["content"] == "hi"
    assert out[1]["content"] == "Looking. " and out[1]["toolCalls"][0]["id"] == "c1"
    assert out[2]["toolCallId"] == "c1" and out[3] == {"id": "a1.1", "role": "assistant", "content": "Done."}
