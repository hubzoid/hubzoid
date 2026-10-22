"""Chat inside an app over AG-UI (hubzoid.embed.agui), on the scripted runtime."""
from __future__ import annotations

import json
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
    from hubzoid.embed import config

    config.reset()
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
