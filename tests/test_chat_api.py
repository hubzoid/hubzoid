"""The web app chat API end to end, in-process: server.build_app() on a scratch
hub (``hubzoid init``) with the scripted test runtime."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.chat_helpers import (
    ORIGIN,
    as_people,
    build_app,
    chat_body,
    events,
    make_hub,
    reply_text,
    wait_titles,
    who,
)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def app(hub):
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def agent(app):
    return app.state.chat.model_label


@pytest.fixture
def prompts(app, monkeypatch):
    """Record every prompt the runtime is given."""
    seen: list[str] = []
    runtime = app.state.chat.runtime
    original = runtime.stream_events

    def recording(prompt):
        seen.append(prompt)
        return original(prompt)

    monkeypatch.setattr(runtime, "stream_events", recording)
    return seen


def _send(client, conv, text_, agent, mid, aid, parent=None, headers=ORIGIN, **kw):
    return client.post("/api/chat", headers=headers,
                       json=chat_body(conv, text_, agent=agent, message_id=mid, parent_id=parent,
                                      assistant_id=aid, **kw))


def _messages(client, conv, headers=ORIGIN):
    return {m["id"]: m for m in client.get(f"/api/conversations/{conv}", headers=headers).json()["messages"]}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
def test_create_conversation(client, agent):
    r = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "id": "c_create001"})
    assert r.status_code == 201
    conv = r.json()["conversation"]
    assert conv["id"] == "c_create001" and conv["agent"] == agent and conv["hub"] == "chat-hub"
    assert conv["api_base"] == "" and conv["archived"] is False and conv["title"] is None
    # the same request again (a retry) returns the same conversation
    again = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "id": "c_create001"})
    assert again.status_code == 200 and again.json()["conversation"]["id"] == "c_create001"
    # without an id the server makes one
    made = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent}).json()
    assert made["conversation"]["id"].startswith("c_")


def test_create_needs_this_bridges_agent_and_a_valid_id(client, agent):
    r = client.post("/api/conversations", headers=ORIGIN, json={"agent": "someone-else"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "agent_not_found"
    r = client.post("/api/conversations", headers=ORIGIN, json={})
    assert r.json()["detail"]["code"] == "agent_required"
    for bad in ("short", "c_trailing_", "has spaces here", "x" * 65):
        r = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "id": bad})
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_id", bad


def test_first_message_streams_and_stores_the_reply(client, agent):
    r = _send(client, "c_first0001", "Hello there", agent, "m_user00001", "m_asst00001")
    assert r.status_code == 200
    assert r.headers["x-vercel-ai-ui-message-stream"] == "v1"
    assert r.headers["content-type"].startswith("text/event-stream")
    evs = events(r.text)
    assert evs[0] == {"type": "start", "messageId": "m_asst00001",
                      "messageMetadata": {"conversationId": "c_first0001"}}
    assert evs[1] == {"type": "start-step"}
    assert evs[2] == {"type": "data-title", "data": {"title": "Hello there"}, "transient": True}
    assert evs[-3:] == [{"type": "finish-step"},
                        {"type": "finish", "messageMetadata": {"status": "complete"}}, "[DONE]"]
    answer = reply_text(evs)
    assert answer.startswith("You said: “Hello there”")

    got = client.get("/api/conversations/c_first0001", headers=ORIGIN).json()
    assert got["head_id"] == "m_asst00001"
    user, reply = got["messages"]
    assert user == {"id": "m_user00001", "parent_id": None, "role": "user",
                    "content": [{"type": "text", "text": "Hello there"}], "status": "complete",
                    "error": None, "created_at": user["created_at"]}
    assert reply["parent_id"] == "m_user00001" and reply["role"] == "assistant"
    assert reply["status"] == "complete" and reply["content"] == [{"type": "text", "text": answer}]
    wait_titles()
    conv = client.get("/api/conversations/c_first0001", headers=ORIGIN).json()["conversation"]
    assert conv["title"] == "About hello there" and conv["title_source"] == "auto"


def test_second_turn_wire_format(client, agent):
    _send(client, "c_wire00001", "one", agent, "m_user00001", "m_asst00001")
    wait_titles()
    r = _send(client, "c_wire00001", "two", agent, "m_user00002", "m_asst00002",
              parent="m_asst00001")
    reply = "You said: “two”\n\nThis is a **scripted** reply from the Hubzoid test runtime."
    import re
    deltas = "".join(
        'data: {"type":"text-delta","id":"t1","delta":' + json.dumps(p, ensure_ascii=False) + "}\n\n"
        for p in re.findall(r"\S+\s*|\s+", reply))
    assert r.text == (
        'data: {"type":"start","messageId":"m_asst00002","messageMetadata":{"conversationId":"c_wire00001"}}\n\n'
        'data: {"type":"start-step"}\n\n'
        'data: {"type":"text-start","id":"t1"}\n\n'
        + deltas +
        'data: {"type":"text-end","id":"t1"}\n\n'
        'data: {"type":"finish-step"}\n\n'
        'data: {"type":"finish","messageMetadata":{"status":"complete"}}\n\n'
        "data: [DONE]\n\n")


def test_tools_and_reasoning_are_structured(hub, monkeypatch):
    monkeypatch.setenv("SHOW_THINKING", "full")
    app = build_app()
    client = TestClient(app)
    r = _send(client, "c_tools0001", "think, use a tool, then fail", app.state.chat.model_label,
              "m_user00001", "m_asst00001")
    evs = events(r.text)
    types = [e["type"] for e in evs if isinstance(e, dict)]
    assert types.count("tool-input-available") == 2
    assert {"type": "tool-output-available", "toolCallId": "call_tool", "output": {"status": "ok"}} in evs
    assert any(e.get("type") == "tool-output-error" and e["toolCallId"] == "call_fail"
               for e in evs if isinstance(e, dict))
    stored = _messages(client, "c_tools0001")["m_asst00001"]["content"]
    assert [p["type"] for p in stored] == ["reasoning", "tool-call", "tool-call", "text"]
    assert stored[1]["result"] == {"status": "ok"} and stored[2]["result"]["status"] == "error"


def test_show_tools_off_hides_tool_entries(hub, monkeypatch):
    monkeypatch.setenv("SHOW_TOOLS", "off")
    app = build_app()
    client = TestClient(app)
    r = _send(client, "c_notools01", "use a tool", app.state.chat.model_label, "m_user00001",
              "m_asst00001")
    assert not [e for e in events(r.text) if isinstance(e, dict) and e["type"].startswith("tool-")]
    assert [p["type"] for p in _messages(client, "c_notools01")["m_asst00001"]["content"]] == ["text"]


def test_run_error_is_reported_and_stored(client, agent):
    r = _send(client, "c_error0001", "please error", agent, "m_user00001", "m_asst00001")
    evs = events(r.text)
    assert {"type": "error", "errorText": "RuntimeError: scripted failure"} in evs
    assert evs[-2] == {"type": "finish", "messageMetadata": {"status": "error"}}
    reply = _messages(client, "c_error0001")["m_asst00001"]
    assert reply["status"] == "error" and reply["error"] == "RuntimeError: scripted failure"
    assert reply["content"] == [{"type": "text", "text": "Starting on that. "}]


# ---------------------------------------------------------------------------
# Branches: history, edit, regenerate, idempotency
# ---------------------------------------------------------------------------
def test_history_edit_and_regenerate(client, agent, prompts):
    _send(client, "c_branch001", "first", agent, "m_user00001", "m_asst00001")
    _send(client, "c_branch001", "second", agent, "m_user00002", "m_asst00002", parent="m_asst00001")
    first_reply = _messages(client, "c_branch001")["m_asst00001"]["content"][0]["text"]
    assert prompts[-1] == f"[user]\nfirst\n\n[assistant]\n{first_reply}\n\n[user]\nsecond"

    # edit "second": a new user message beside it, on the same parent
    r = _send(client, "c_branch001", "second, edited", agent, "m_user00002b", "m_asst00002b",
              parent="m_asst00001")
    assert r.status_code == 200
    assert prompts[-1] == f"[user]\nfirst\n\n[assistant]\n{first_reply}\n\n[user]\nsecond, edited"

    # regenerate the first reply: a new assistant message beside it
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_branch001", "parent_id": "m_user00001",
        "assistant_message_id": "m_asst00001b"})
    assert r.status_code == 200 and events(r.text)[0]["messageId"] == "m_asst00001b"
    assert prompts[-1] == "[user]\nfirst"

    got = client.get("/api/conversations/c_branch001", headers=ORIGIN).json()
    parents = {m["id"]: m["parent_id"] for m in got["messages"]}
    assert parents == {"m_user00001": None, "m_asst00001": "m_user00001",
                       "m_user00002": "m_asst00001", "m_asst00002": "m_user00002",
                       "m_user00002b": "m_asst00001", "m_asst00002b": "m_user00002b",
                       "m_asst00001b": "m_user00001"}
    assert got["head_id"] == "m_asst00001b"


def test_user_message_is_idempotent_by_id(client, agent):
    _send(client, "c_idem00001", "hello", agent, "m_user00001", "m_asst00001")
    r = _send(client, "c_idem00001", "hello (retried)", agent, "m_user00001", "m_asst00002")
    assert r.status_code == 200
    msgs = _messages(client, "c_idem00001")
    assert msgs["m_user00001"]["content"] == [{"type": "text", "text": "hello"}]
    assert msgs["m_asst00002"]["parent_id"] == "m_user00001"
    assert len(msgs) == 3


def test_ids_already_used_are_conflicts(client, agent):
    _send(client, "c_ids000001", "hello", agent, "m_user00001", "m_asst00001")
    # a message id from another conversation
    r = _send(client, "c_ids000002", "hi", agent, "m_user00001", "m_asst00009")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "id_conflict"
    # a reply id already used
    r = _send(client, "c_ids000001", "again", agent, "m_user00002", "m_asst00001",
              parent="m_asst00001")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "id_conflict"
    # the reply can not reuse the message's id
    r = _send(client, "c_ids000001", "same", agent, "m_same00001", "m_same00001",
              parent="m_asst00001")
    assert r.status_code == 400
    # the same user id with a different parent
    r = _send(client, "c_ids000001", "moved", agent, "m_user00001", "m_asst00003",
              parent="m_asst00001")
    assert r.status_code == 409


def test_parent_rules(client, agent):
    _send(client, "c_parent001", "hello", agent, "m_user00001", "m_asst00001")
    r = _send(client, "c_parent001", "x", agent, "m_user00002", "m_asst00002", parent="m_user00001")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_parent"
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_parent001", "parent_id": "m_asst00001"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_parent"
    r = _send(client, "c_parent001", "x", agent, "m_user00003", "m_asst00003", parent="m_nothere01")
    assert r.status_code == 400
    # a new conversation starts with a message, at the root
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_parent002", "agent": agent, "parent_id": "m_user00001"})
    assert r.status_code == 400


def test_message_validation(client, agent):
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_valid0001", "agent": agent,
        "message": {"id": "m_user00001", "content": [{"type": "text", "text": ""}]}})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "empty_message"
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_valid0001", "agent": agent,
        "message": {"id": "m_user00001", "content": [{"type": "audio", "data": "x"}]}})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_message"
    r = client.post("/api/chat", headers=ORIGIN, json={
        "conversation_id": "c_valid0001", "agent": agent,
        "message": {"id": "m_user00001", "content": [{"type": "file", "file_id": "nope.txt"}]}})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "file_not_found"
    r = client.post("/api/chat", headers=ORIGIN, content=b"not json")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_json"
    r = _send(client, "c_valid0002", "hi", None, "m_user00002", "m_asst00002")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "agent_required"
    r = _send(client, "c_valid0002", "hi", "someone-else", "m_user00002", "m_asst00002")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "agent_not_found"


def test_one_reply_at_a_time_per_conversation(app, client, agent):
    _send(client, "c_busy00001", "hello", agent, "m_user00001", "m_asst00001")
    assert app.state.chat.runs.claim("c_busy00001")   # a reply in progress
    r = _send(client, "c_busy00001", "again", agent, "m_user00002", "m_asst00002",
              parent="m_asst00001")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "run_in_progress"
    app.state.chat.runs.release("c_busy00001")
    assert _send(client, "c_busy00001", "again", agent, "m_user00002", "m_asst00002",
                 parent="m_asst00001").status_code == 200


# ---------------------------------------------------------------------------
# List, search, pages, updates, delete
# ---------------------------------------------------------------------------
def test_list_search_and_pages(app, client, agent):
    for n, words in enumerate(["quarterly budget", "holiday rota", "budget follow-up"]):
        _send(client, f"c_list0000{n}", words, agent, f"m_user0000{n}", f"m_asst0000{n}")
    wait_titles()
    items = client.get("/api/conversations").json()["items"]
    assert [c["id"] for c in items] == ["c_list00002", "c_list00001", "c_list00000"]
    found = client.get("/api/conversations", params={"q": "BUDGET"}).json()["items"]
    assert [c["id"] for c in found] == ["c_list00002", "c_list00000"]
    in_reply = client.get("/api/conversations", params={"q": "scripted"}).json()["items"]
    assert len(in_reply) == 3   # assistant text is searchable too
    page1 = client.get("/api/conversations", params={"limit": 2}).json()
    page2 = client.get("/api/conversations", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [c["id"] for c in page1["items"] + page2["items"]] == [c["id"] for c in items]
    assert page2["next_cursor"] is None
    assert client.get("/api/conversations", params={"cursor": "junk"}).status_code == 400
    assert client.get("/api/conversations", params={"limit": "many"}).status_code == 400


def test_patch_title_archive_and_head(client, agent):
    _send(client, "c_patch0001", "hello", agent, "m_user00001", "m_asst00001")
    wait_titles()
    r = client.patch("/api/conversations/c_patch0001", headers=ORIGIN, json={"title": "  My   notes "})
    assert r.json()["conversation"]["title"] == "My notes"
    assert r.json()["conversation"]["title_source"] == "user"
    r = client.patch("/api/conversations/c_patch0001", headers=ORIGIN, json={"archived": True})
    assert r.json()["conversation"]["archived"] is True
    assert client.get("/api/conversations").json()["items"] == []
    assert [c["id"] for c in client.get("/api/conversations?archived=1").json()["items"]] == ["c_patch0001"]
    r = client.patch("/api/conversations/c_patch0001", headers=ORIGIN, json={"head_id": "m_user00001"})
    assert r.json()["conversation"]["head_id"] == "m_user00001"
    assert client.get("/api/conversations/c_patch0001").json()["head_id"] == "m_user00001"
    for bad in ({"head_id": "m_nothere01"}, {"title": ""}, {"title": "x" * 201}, {"archived": "yes"}):
        assert client.patch("/api/conversations/c_patch0001", headers=ORIGIN, json=bad).status_code == 400


def test_delete_removes_messages_shares_and_files(hub, client, agent):
    from hubzoid import memory

    up = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "id": "c_delete001"})
    assert up.status_code == 201
    f = client.post("/api/conversations/c_delete001/files", headers=ORIGIN,
                    files={"file": ("notes.txt", b"hello", "text/plain")}).json()
    _send(client, "c_delete001", "make an artifact", agent, "m_user00001", "m_asst00001",
          files=(f["file_id"],))
    client.post("/api/conversations/c_delete001/share", headers=ORIGIN)
    folder = memory.chat_root(hub, "c_delete001")
    assert (folder / "uploads" / "notes.txt").is_file()
    assert (folder / "artifacts" / "scripted-report.md").is_file()
    r = client.delete("/api/conversations/c_delete001", headers=ORIGIN)
    assert r.status_code == 204
    assert not folder.exists()
    assert client.get("/api/conversations/c_delete001").status_code == 404
    store = client.app.state.chat.store
    assert store.get_message("m_user00001") is None and store.share_for("c_delete001") is None


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------
def test_sign_in_is_required(hub, monkeypatch):
    as_people(monkeypatch)
    client = TestClient(build_app())
    r = client.get("/api/conversations")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "unauthenticated"
    assert client.post("/api/chat", headers=ORIGIN, json={}).status_code == 401


def test_conversations_are_private_to_their_owner(hub, monkeypatch):
    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    agent = app.state.chat.model_label
    assert _send(client, "c_anas00001", "mine", agent, "m_user00001", "m_asst00001",
                 headers=who("ana")).status_code == 200
    ben = who("ben")
    assert client.get("/api/conversations", headers=ben).json()["items"] == []
    for method, path, body in [
        ("GET", "/api/conversations/c_anas00001", None),
        ("PATCH", "/api/conversations/c_anas00001", {"title": "x"}),
        ("DELETE", "/api/conversations/c_anas00001", None),
        ("GET", "/api/runs/m_asst00001", None),
        ("POST", "/api/runs/m_asst00001/cancel", None),
        ("GET", "/api/conversations/c_anas00001/share", None),
        ("POST", "/api/conversations/c_anas00001/share", None),
    ]:
        r = client.request(method, path, headers=ben, json=body)
        assert r.status_code == 404, (method, path, r.text)
    r = _send(client, "c_anas00001", "intrude", agent, "m_user00002", "m_asst00002",
              parent="m_asst00001", headers=ben)
    assert r.status_code == 404
    # the owner still sees everything
    assert client.get("/api/conversations/c_anas00001", headers=who("ana")).status_code == 200


def test_mutations_need_our_origin(client, agent):
    r = client.post("/api/conversations", json={"agent": agent})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "cross_origin"
    r = client.post("/api/chat", headers={"Origin": "https://evil.example"},
                    json=chat_body("c_csrf00001", "x", agent=agent, message_id="m_user00001"))
    assert r.status_code == 403
    r = client.delete("/api/conversations/c_csrf00001", headers={"Referer": "https://evil.example/x"})
    assert r.status_code == 403
    assert client.get("/api/conversations").status_code == 200   # reads need no Origin


def test_hub_scoped_calls_on_another_hubs_conversation(app, client):
    store = app.state.chat.store
    store.create_conversation(conv_id="c_otherhub1", owner_id="local-owner",
                              owner_email="admin@localhost", hub="other-hub", agent="other-agent")
    store.insert_message(message_id="m_other0001", conversation_id="c_otherhub1", parent_id=None,
                         role="user", content=[{"type": "text", "text": "x"}], text="x")
    for method, path, kw in [
        ("POST", "/api/chat", {"json": chat_body("c_otherhub1", "hi", agent=None,
                                                 message_id="m_user00001")}),
        ("DELETE", "/api/conversations/c_otherhub1", {}),
        ("POST", "/api/conversations/c_otherhub1/files",
         {"files": {"file": ("a.txt", b"a", "text/plain")}}),
        ("GET", "/api/conversations/c_otherhub1/files/a.txt", {}),
        ("GET", "/api/runs/m_other0001", {}),
    ]:
        r = client.request(method, path, headers=ORIGIN, **kw)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "wrong_hub", (method, path)
    # deployment-wide calls work from any bridge
    assert client.get("/api/conversations/c_otherhub1").status_code == 200
    assert client.patch("/api/conversations/c_otherhub1", headers=ORIGIN,
                        json={"title": "Renamed"}).status_code == 200


def test_hub_access_is_enforced(hub, monkeypatch):
    from hubzoid import access

    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    agent = app.state.chat.model_label
    gs = access.store_for(hub)
    gs.set_authoritative(True, hub=hub.name)
    gs.grant("ana@example.org", hub.name, "use_hub", actor="test")
    assert _send(client, "c_access001", "hi", agent, "m_user00001", "m_asst00001",
                 headers=who("ana")).status_code == 200
    r = _send(client, "c_access002", "hi", agent, "m_user00002", "m_asst00002", headers=who("ben"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "no_access"
    assert "ben@example.org" in r.json()["detail"]["message"]
    r = client.post("/api/conversations", headers=who("ben"), json={"agent": agent})
    assert r.status_code == 403
    gs.suspend("ana@example.org", actor="admin@example.org")
    r = _send(client, "c_access001", "again", agent, "m_user00003", "m_asst00003",
              parent="m_asst00001", headers=who("ana"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "suspended"


def test_files_per_message_limit(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_MAX_FILES_PER_MESSAGE", "2")
    app = build_app()
    client = TestClient(app)
    agent = app.state.chat.model_label
    client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "id": "c_files0001"})
    ids = [client.post("/api/conversations/c_files0001/files", headers=ORIGIN,
                       files={"file": (f"f{n}.txt", b"x", "text/plain")}).json()["file_id"]
           for n in range(3)]
    r = _send(client, "c_files0001", "three", agent, "m_user00001", "m_asst00001", files=tuple(ids))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "too_many_files"
    r = _send(client, "c_files0001", "two", agent, "m_user00001", "m_asst00001", files=tuple(ids[:2]))
    assert r.status_code == 200
    assert "Attached: `f0.txt`, `f1.txt`." in reply_text(events(r.text))


# ---------------------------------------------------------------------------
# Usage, and the OpenAI-compatible path on the same runtime
# ---------------------------------------------------------------------------
def test_usage_rows_for_the_reply_and_the_title(app, client, agent):
    from hubzoid import db

    _send(client, "c_usage0001", "count this", agent, "m_user00001", "m_asst00001")
    wait_titles()
    with db.operational_engine(app.state.chat.hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT surface, kind, subject, chat_id, model, status, "
                                 "input_tokens, output_tokens FROM hz_usage ORDER BY kind")).fetchall()
    kinds = {r[1]: r for r in rows}
    assert set(kinds) == {"chat", "background"}
    chat = kinds["chat"]
    assert chat[0] == "web" and chat[2] == "admin@localhost" and chat[3] == "c_usage0001"
    assert chat[4] == "hubzoid-test/scripted" and chat[5] == "ok" and chat[7] > 0
    assert kinds["background"][0] == "web" and kinds["background"][3] == "c_usage0001"
    reply = app.state.chat.store.get_message("m_asst00001")
    assert reply["model"] == "hubzoid-test/scripted" and reply["usage"]["output_tokens"] > 0


def test_openai_compatible_endpoint_keeps_its_text(client):
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer k-chat-test"},
                    json={"model": "x", "messages": [{"role": "user", "content": "use a tool"}]})
    assert r.status_code == 200
    content = r.json()["choices"][0]["message"]["content"]
    assert "<summary>↳ read_knowledge</summary>" in content
    assert "You said: “use a tool”" in content
