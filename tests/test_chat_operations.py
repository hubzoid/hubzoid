"""The chat operation engine (hubzoid.chat.engine) through its HTTP API, on a
scratch hub with the scripted test runtime: placement rules, idempotent
operations, save-only edits, branches and views, queue, stop, deletion, fork,
synthesis, continue, events and the body cap."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch, HUBZOID_TEST_SLOW_SECONDS="3")


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def h(headers=ORIGIN, key=None):
    return {**headers, "Idempotency-Key": key or str(uuid.uuid4())}


def new_conv(client, headers=ORIGIN):
    agent = client.app.state.chat.model_label
    r = client.post("/api/conversations", headers=headers, json={"agent": agent})
    assert r.status_code == 201, r.text
    return r.json()["conversation"]["id"], r.json()["branch"]


def op(client, cid, body, headers=ORIGIN, key=None):
    return client.post(f"/api/conversations/{cid}/operations", headers=h(headers, key), json=body)


def wait_run(client, run_id, headers=ORIGIN, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/chat/runs/{run_id}", headers=headers).json()
        if r["status"] in ("complete", "stopped", "interrupted", "failed"):
            return r
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def send(client, cid, text, branch, headers=ORIGIN, parent=None):
    r = op(client, cid, {"kind": "send", "branch_id": branch["id"], "expected_revision": branch["revision"],
                         "parent_id": parent, "content": [{"type": "text", "text": text}]}, headers)
    assert r.status_code == 202, r.text
    return r.json()


def snap(client, cid, headers=ORIGIN):
    return client.get(f"/api/conversations/{cid}", headers=headers).json()


def branch_of(client, cid, bid, headers=ORIGIN):
    return next(b for b in snap(client, cid, headers)["branches"] if b["id"] == bid)


def test_same_operation_returns_receipt(client):
    cid, b = new_conv(client)
    body = {"kind": "send", "branch_id": b["id"], "expected_revision": 1, "parent_id": None,
            "content": [{"type": "text", "text": "hello"}]}
    first = op(client, cid, body, key="k-same-1")
    again = op(client, cid, body, key="k-same-1")
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()
    wait_run(client, first.json()["run_id"])
    assert len(snap(client, cid)["messages"]) == 2
    receipt = client.get(f"/api/chat/operations/{first.json()['operation_id']}").json()
    assert receipt["state"] == "committed" and receipt["result"]["run_id"] == first.json()["run_id"]


def test_operation_payload_mismatch(client):
    cid, b = new_conv(client)
    body = {"kind": "send", "branch_id": b["id"], "expected_revision": 1, "parent_id": None,
            "content": [{"type": "text", "text": "hello"}]}
    wait_run(client, op(client, cid, body, key="k-mm").json()["run_id"])
    other = op(client, cid, {**body, "content": [{"type": "text", "text": "different"}]}, key="k-mm")
    assert other.status_code == 409 and other.json()["detail"]["code"] == "idempotency_key_reused"
    no_key = client.post(f"/api/conversations/{cid}/operations", headers=ORIGIN, json=body)
    assert no_key.status_code == 400


def test_send_requires_branch_head(client):
    cid, b = new_conv(client)
    first = send(client, cid, "one", b)
    wait_run(client, first["run_id"])
    b = branch_of(client, cid, b["id"])
    stale = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"],
                             "parent_id": first["user_message_id"], "content": "two"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "branch_conflict"
    old_rev = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": 1,
                               "parent_id": first["assistant_message_id"], "content": "two"})
    assert old_rev.status_code == 409 and old_rev.json()["detail"]["current_revision"] == b["revision"]


def test_non_writer_extend_returns_branch_conflict(client, hub, monkeypatch):
    cid, b = new_conv(client)
    wait_run(client, send(client, cid, "one", b)["run_id"])
    # Ana becomes an editor of the owner's conversation.
    from hubzoid.chat import graph

    with client.app.state.chat.engine.engine.begin() as conn:
        conn.execute(graph.members.insert().values(conversation_id=cid, principal_type="user",
                                                   principal_id="u_ana", role="editor", added_by="t",
                                                   created_at=time.time()))
    as_people(monkeypatch)
    b = branch_of(client, cid, b["id"], who("ana"))
    r = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"],
                         "parent_id": b["head_id"], "content": "mine"}, who("ana"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "branch_conflict"
    own = client.post(f"/api/conversations/{cid}/branches", headers=h(who("ana")),
                      json={"from_message_id": b["head_id"]}).json()["branch"]
    assert own["writer_actor_id"] == "u_ana"
    wait_run(client, send(client, cid, "mine", own, who("ana"), parent=b["head_id"])["run_id"], who("ana"))
    # The owner's view did not move.
    assert snap(client, cid, who("ana"))["view"]["branch_id"] == own["id"]


def test_save_only_has_zero_model_calls(client, monkeypatch):
    cid, b = new_conv(client)
    first = send(client, cid, "list three checks", b)
    wait_run(client, first["run_id"])
    runtime = client.app.state.chat.runtime
    calls = []
    original = runtime.stream_events
    monkeypatch.setattr(runtime, "stream_events", lambda p: calls.append(p) or original(p))
    r = op(client, cid, {"kind": "revise", "target_message_id": first["user_message_id"], "mode": "save",
                         "content": "list five checks"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revises_id"] == first["user_message_id"] and "run_id" not in out and calls == []
    s = snap(client, cid)
    new = next(m for m in s["messages"] if m["id"] == out["message_id"])
    assert new["parent_id"] is None and new["editor_id"] and s["view"]["branch_id"] == out["branch_id"]
    assert branch_of(client, cid, b["id"])["head_id"] == first["assistant_message_id"]  # original untouched
    # respond answers the saved correction without a second user message.
    nb = branch_of(client, cid, out["branch_id"])
    resp = op(client, cid, {"kind": "respond", "branch_id": nb["id"], "expected_revision": nb["revision"],
                            "target_message_id": out["message_id"]})
    assert resp.status_code == 202
    wait_run(client, resp.json()["run_id"])
    assert sum(1 for m in snap(client, cid)["messages"] if m["role"] == "user") == 2


def test_assistant_revision_cannot_forge_tool_result(client):
    cid, b = new_conv(client)
    first = send(client, cid, "use a tool please", b)
    wait_run(client, first["run_id"])
    reply = next(m for m in snap(client, cid)["messages"] if m["id"] == first["assistant_message_id"])
    tools_before = [p for p in reply["content"] if p.get("type") != "text"]
    assert tools_before
    r = op(client, cid, {"kind": "revise", "target_message_id": reply["id"], "mode": "save",
                         "content": "Corrected answer."})
    assert r.status_code == 200
    edited = next(m for m in snap(client, cid)["messages"] if m["id"] == r.json()["message_id"])
    assert [p for p in edited["content"] if p.get("type") != "text"] == tools_before
    assert edited["author_id"] != edited["editor_id"] or edited["editor_id"]
    gen = op(client, cid, {"kind": "revise", "target_message_id": reply["id"], "mode": "generate",
                           "content": "x"})
    assert gen.status_code == 400


def test_regenerate_and_continue_create_actor_branch(client):
    cid, b = new_conv(client)
    first = send(client, cid, "slow answer please", b)
    time.sleep(0.6)
    stop = client.post(f"/api/runs/{first['run_id']}/cancel", headers=ORIGIN, json={"park_queue": True})
    assert stop.status_code in (200, 202)
    run = wait_run(client, first["run_id"])
    assert run["status"] == "stopped" and run["message"]["status"] == "cancelled"
    prefix = "".join(p["text"] for p in run["message"]["content"] if p["type"] == "text")
    assert prefix
    regen = op(client, cid, {"kind": "regenerate", "target_message_id": first["user_message_id"],
                             "refinement": "shorter"})
    cont = op(client, cid, {"kind": "continue", "target_message_id": first["assistant_message_id"]})
    assert regen.status_code == cont.status_code == 202, (regen.text, cont.text)
    branches = {b_["id"] for b_ in snap(client, cid)["branches"]}
    assert {regen.json()["branch_id"], cont.json()["branch_id"]} <= branches
    assert regen.json()["branch_id"] != b["id"] and cont.json()["branch_id"] != b["id"]
    wait_run(client, regen.json()["run_id"])
    done = wait_run(client, cont.json()["run_id"])
    text = "".join(p["text"] for p in done["message"]["content"] if p["type"] == "text")
    assert text.startswith(prefix) and text.count(prefix) == 1  # test_continue_prefix_once
    assert branch_of(client, cid, b["id"])["head_id"] == first["assistant_message_id"]


def test_parallel_branches_admit_concurrently(client):
    cid, b = new_conv(client)
    first = send(client, cid, "start", b)
    wait_run(client, first["run_id"])
    a1 = client.post(f"/api/conversations/{cid}/branches", headers=h(),
                     json={"from_message_id": first["assistant_message_id"]}).json()["branch"]
    a2 = client.post(f"/api/conversations/{cid}/branches", headers=h(),
                     json={"from_message_id": first["assistant_message_id"]}).json()["branch"]
    r1 = send(client, cid, "slow tradeoffs", a1, parent=first["assistant_message_id"])
    r2 = send(client, cid, "slow checklist", a2, parent=first["assistant_message_id"])
    runs = {r["id"]: r["status"] for r in snap(client, cid)["runs"]}
    assert set(runs) == {r1["run_id"], r2["run_id"]}
    # One branch never has two writers.
    a1now = branch_of(client, cid, a1["id"])
    busy = op(client, cid, {"kind": "send", "branch_id": a1["id"], "expected_revision": a1now["revision"],
                            "parent_id": a1now["head_id"], "content": "more"})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] in ("branch_conflict", "branch_busy")
    assert wait_run(client, r1["run_id"])["status"] == "complete"
    assert wait_run(client, r2["run_id"])["status"] == "complete"


def test_view_change_is_personal(client, monkeypatch):
    cid, b = new_conv(client)
    first = send(client, cid, "hi", b)
    wait_run(client, first["run_id"])
    from hubzoid.chat import graph

    with client.app.state.chat.engine.engine.begin() as conn:
        conn.execute(graph.members.insert().values(conversation_id=cid, principal_type="user",
                                                   principal_id="u_ana", role="viewer", added_by="t",
                                                   created_at=time.time()))
    before = snap(client, cid)
    as_people(monkeypatch)
    r = client.put(f"/api/conversations/{cid}/view", headers=who("ana"),
                   json={"branch_id": b["id"], "view_head_id": first["user_message_id"]})
    assert r.status_code == 200 and r.json()["view_head_id"] == first["user_message_id"]
    assert snap(client, cid, who("ana"))["view"]["view_head_id"] == first["user_message_id"]
    rev = before["conversation"]["graph_revision"]
    before = before["view"]
    monkeypatch.undo()
    after = snap(client, cid)
    assert after["view"]["view_head_id"] == before["view_head_id"]
    assert after["conversation"]["graph_revision"] == rev
    # A viewer can't write.
    as_people(monkeypatch)
    denied = op(client, cid, {"kind": "regenerate", "target_message_id": first["user_message_id"]}, who("ana"))
    assert denied.status_code == 403


def test_queue_order_stop_parks_and_removed_predecessor_parks_successors(client):
    cid, b = new_conv(client)
    first = send(client, cid, "slow first", b)
    items = []
    for text in ("two", "three", "four"):
        r = client.post(f"/api/conversations/{cid}/queue", headers=h(),
                        json={"branch_id": b["id"], "content": text,
                              "predecessor_id": items[-1]["id"] if items else None})
        assert r.status_code == 201, r.text
        items.append(r.json()["item"])
    assert [i["state"] for i in items] == ["queued"] * 3
    # Removing the middle one parks what depends on it.
    rm = client.patch(f"/api/conversations/{cid}/queue/{items[1]['id']}", headers=ORIGIN,
                      json={"action": "remove", "expected_revision": 1})
    assert rm.status_code == 200 and rm.json()["state"] == "removed"
    q = {i["id"]: i for i in client.get(f"/api/conversations/{cid}/queue").json()["items"]}
    assert q[items[2]["id"]]["state"] == "blocked"
    # Stop parks the queue before cancelling; partial text stays.
    client.post(f"/api/runs/{first['run_id']}/cancel", headers=ORIGIN, json={"park_queue": True})
    assert wait_run(client, first["run_id"])["status"] == "stopped"
    q = {i["id"]: i for i in client.get(f"/api/conversations/{cid}/queue").json()["items"]}
    assert q[items[0]["id"]]["state"] == "paused"
    bnow = branch_of(client, cid, b["id"])
    assert bnow["queue_paused"]
    # Resume dispatches the next item in order.
    res = client.post(f"/api/conversations/{cid}/queue-control", headers=ORIGIN,
                      json={"branch_id": b["id"], "action": "resume", "expected_revision": bnow["queue_revision"]})
    assert res.status_code == 200
    end = time.time() + 15
    while time.time() < end:
        texts = [m for m in snap(client, cid)["messages"] if m["role"] == "user"]
        if len(texts) == 2 and not snap(client, cid)["runs"]:
            break
        time.sleep(0.1)
    users = [m for m in snap(client, cid)["messages"] if m["role"] == "user"]
    assert [u["content"][0]["text"] for u in users] == ["slow first", "two"]


def test_delete_reparents_grandchildren(client):
    cid, b = new_conv(client)
    one = send(client, cid, "one", b)
    wait_run(client, one["run_id"])
    b = branch_of(client, cid, b["id"])
    two = send(client, cid, "two", b, parent=one["assistant_message_id"])
    wait_run(client, two["run_id"])
    rev = snap(client, cid)["conversation"]["graph_revision"]
    prev = client.post(f"/api/conversations/{cid}/deletion-preview", headers=ORIGIN,
                       json={"message_id": one["assistant_message_id"], "expected_revision": rev}).json()
    assert prev["removed"] == [one["assistant_message_id"], two["user_message_id"]]
    assert prev["reparented"] == {two["assistant_message_id"]: one["user_message_id"]}
    bad = op(client, cid, {"kind": "delete", "target_message_id": one["assistant_message_id"],
                           "expected_revision": rev, "preview_hash": "sha256:00"})
    assert bad.status_code == 409
    ok = op(client, cid, {"kind": "delete", "target_message_id": one["assistant_message_id"],
                          "expected_revision": rev, "preview_hash": prev["preview_hash"]})
    assert ok.status_code == 200, ok.text
    msgs = {m["id"]: m for m in snap(client, cid)["messages"]}
    assert set(msgs) == {one["user_message_id"], two["assistant_message_id"]}
    assert msgs[two["assistant_message_id"]]["parent_id"] == one["user_message_id"]
    assert msgs[two["assistant_message_id"]]["context_changed"] is True
    # test_deleted_content_removed_from_replay
    from hubzoid.chat import graph

    events = graph.events_after(client.app.state.chat.engine.engine, cid, 0)
    about_removed = [e for e in events if e["payload"].get("message_id") in prev["removed"]
                     or e["object_id"] in prev["removed"]]
    assert about_removed and all("message" not in e["payload"] for e in about_removed)


def test_fork_maps_ids_without_authority(client):
    cid, b = new_conv(client)
    one = send(client, cid, "one", b)
    wait_run(client, one["run_id"])
    r = op(client, cid, {"kind": "fork", "source_head_id": one["assistant_message_id"], "scope": "path"})
    assert r.status_code == 201, r.text
    out = r.json()
    new = snap(client, out["new_conversation_id"])
    assert len(new["messages"]) == 2 and not new["runs"] and not new["queue"]
    assert set(out["mapping"]) == {one["user_message_id"], one["assistant_message_id"]}
    assert all(m["id"] not in out["mapping"] for m in new["messages"])
    assert new["view"]["view_head_id"] == out["mapping"][one["assistant_message_id"]]


def test_synthesize_quotes_sources_as_data(client):
    cid, b = new_conv(client)
    one = send(client, cid, "one", b)
    wait_run(client, one["run_id"])
    regen = op(client, cid, {"kind": "regenerate", "target_message_id": one["user_message_id"]}).json()
    wait_run(client, regen["run_id"])
    nb = branch_of(client, cid, regen["branch_id"])
    syn = op(client, cid, {"kind": "synthesize", "branch_id": nb["id"], "expected_revision": nb["revision"],
                           "parent_id": nb["head_id"], "content": "Combine them",
                           "source_message_ids": [one["assistant_message_id"], regen["assistant_message_id"]]})
    assert syn.status_code == 202, syn.text
    wait_run(client, syn.json()["run_id"])
    user = next(m for m in snap(client, cid)["messages"] if m["id"] == syn.json()["user_message_id"])
    assert user["content"][-1] == {"type": "data-synthesis", "data": {"sources": [
        one["assistant_message_id"], regen["assistant_message_id"]]}}


def test_temporary_conversation_expires_and_save_keeps_it(client):
    agent = client.app.state.chat.model_label
    made = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "temporary": True}).json()
    cid = made["conversation"]["id"]
    assert made["conversation"]["temporary"] and made["conversation"]["expires_at"]
    kept = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "temporary": True}).json()
    kid = kept["conversation"]["id"]
    assert client.post(f"/api/conversations/{kid}/save", headers=h(),
                       json={"expected_revision": snap(client, kid)["conversation"]["graph_revision"]}).status_code == 200
    from hubzoid.chat.store import conversations

    eng = client.app.state.chat.engine.engine
    with eng.begin() as conn:
        conn.execute(conversations.update().where(conversations.c.id.in_([cid, kid]))
                     .values(expires_at=time.time() - 1))
    assert client.app.state.chat.engine.expire_temporary() == 1
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.get(f"/api/conversations/{kid}").status_code == 200


def test_events_replay_and_cursor(client):
    cid, b = new_conv(client)
    one = send(client, cid, "hello", b)
    wait_run(client, one["run_id"])
    s = snap(client, cid)
    assert s["cursor"] > 0
    with client.stream("GET", f"/api/conversations/{cid}/events?after=0") as r:
        lines = []
        for line in r.iter_lines():
            lines.append(line)
            if line.startswith("data: ") and any(x == f"id: {s['cursor']}" for x in lines):
                break
    ids = [int(x[4:]) for x in lines if x.startswith("id: ")]
    assert ids == sorted(ids) and ids[-1] == s["cursor"]
    types = [x for x in lines if '"type": "run.changed"' in x]
    assert any('"complete"' in t for t in types)
    from hubzoid.chat import graph

    with client.app.state.chat.engine.engine.begin() as conn:
        conn.execute(graph.events.delete().where(graph.events.c.conversation_id == cid,
                                                 graph.events.c.sequence <= 3))
    gone = client.get(f"/api/conversations/{cid}/events?after=1")
    assert gone.status_code == 410 and gone.json()["detail"]["code"] == "replay_expired"


def test_chat_body_cap_413_before_parse(client):
    cid, b = new_conv(client)
    huge = b"{" + b" " * (9 * 1024 * 1024) + b"}"
    r = client.post(f"/api/conversations/{cid}/operations", headers={**h(), "Content-Type": "application/json"},
                    content=huge)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "request_too_large"
    small = client.put(f"/api/conversations/{cid}/view", headers=ORIGIN, content=b" " * (300 * 1024))
    assert small.status_code == 413


def test_chat_accepts_current_message_limit(client):
    cid, b = new_conv(client)
    text = "é" * 999_000
    r = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_revision": 1, "parent_id": None,
                         "content": [{"type": "text", "text": text}]})
    assert r.status_code == 202, r.text[:200]
    wait_run(client, r.json()["run_id"])


def test_interrupted_run_is_not_rerun(client):
    cid, b = new_conv(client)
    first = send(client, cid, "slow", b)
    eng = client.app.state.chat.engine
    # A restart: the process that held the run is gone.
    assert eng.sweep() >= 1
    from hubzoid.chat import graph

    with eng.engine.connect() as conn:
        status = conn.execute(graph.runs.select().where(graph.runs.c.id == first["run_id"])).first().status
    assert status == "interrupted"
    assert branch_of(client, cid, b["id"])["queue_paused"]


def test_branch_binds_native_session_and_invalidates(client, monkeypatch):
    """B038: a branch resumes the runtime's own session for its next turn; an
    edit or regeneration (a new branch) and a failed run start from full history."""
    from hubzoid import _request_ctx

    runtime = client.app.state.chat.runtime
    original = runtime.stream_events
    seen = []

    def fake(prompt):
        slot = _request_ctx.native_session()
        seen.append((slot.resume_id if slot else None, slot.delta_prompt if slot else None, prompt))
        if slot is not None:
            slot.runtime = "fake"
            slot.session_id = f"sess-{len(seen)}"
        return original(prompt)

    monkeypatch.setattr(runtime, "stream_events", fake)
    cid, b = new_conv(client)
    one = send(client, cid, "first question", b)
    wait_run(client, one["run_id"])
    b = branch_of(client, cid, b["id"])
    two = send(client, cid, "second question", b, parent=one["assistant_message_id"])
    wait_run(client, two["run_id"])
    assert seen[0][0] is None
    assert seen[1][0] == "sess-1" and seen[1][1] == "second question" and "first question" in seen[1][2]
    regen = op(client, cid, {"kind": "regenerate", "target_message_id": two["user_message_id"]}).json()
    wait_run(client, regen["run_id"])
    assert seen[2][0] is None
    # A stale binding (head moved by someone resuming from an earlier message) is not used.
    b = branch_of(client, cid, b["id"])
    three = send(client, cid, "third", b, parent=two["assistant_message_id"])
    wait_run(client, three["run_id"])
    assert seen[3][0] == "sess-2"
