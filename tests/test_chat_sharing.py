"""Collaborators and snapshot links (hubzoid.chat.sharing) through the API."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, PEOPLE, as_people, build_app, make_hub, who
from tests.test_chat_operations import h, new_conv, op, send, snap, wait_run


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub where Ana and Ben have real accounts (so they can be named)."""
    import dataclasses

    from hubzoid.auth import users

    hub = make_hub(tmp_path, monkeypatch)
    for key, p in list(PEOPLE.items()):
        u = users.create(hub, email=p.email, name=p.name)
        monkeypatch.setitem(PEOPLE, key, dataclasses.replace(p, id=u["id"]))
    return hub


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def as_owner_conversation(client):
    cid, b = new_conv(client)
    out = send(client, cid, "the plan", b)
    wait_run(client, out["run_id"])
    return cid, out


def test_snapshot_publish_bound_to_preview_and_revoke(client, monkeypatch):
    cid, out = as_owner_conversation(client)
    pv = client.post("/api/shares/previews", headers=ORIGIN,
                     json={"conversation_id": cid, "head_message_id": out["assistant_message_id"],
                           "audience": {"type": "deployment"}, "allow_copy": True}).json()
    assert pv["projection"]["messages"][0]["content"][0]["text"] == "the plan"
    bad = client.post("/api/shares", headers=ORIGIN, json={"preview_id": pv["preview_id"], "digest": "sha256:00"})
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "snapshot_changed"
    keyed = {**ORIGIN, "Idempotency-Key": "publish-1"}
    made = client.post("/api/shares", headers=keyed, json={"preview_id": pv["preview_id"], "digest": pv["digest"]})
    assert made.status_code == 201, made.text
    share = made.json()
    token = share["url"].rsplit("/", 1)[1]
    again = client.post("/api/shares", headers=keyed, json={"preview_id": pv["preview_id"], "digest": pv["digest"]})
    assert again.json()["id"] == share["id"] and again.json()["url"] == share["url"]  # a keyed retry: same link
    used = client.post("/api/shares", headers=ORIGIN, json={"preview_id": pv["preview_id"], "digest": pv["digest"]})
    assert used.status_code == 404  # a preview publishes once
    # Another hub's manager can't reach this hub's links by id.
    from hubzoid.chat.sharing import SharingService

    svc = client.app.state.chat.sharing
    other = SharingService(svc.hub_dir, svc.engine, "another-hub")
    with pytest.raises(Exception) as exc:
        other.revoke(share["id"], "u_someone", None, manager=True)
    assert getattr(exc.value, "status", None) == 404
    r = client.get(f"/api/shares/{token}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["messages"][1]["role"] == "assistant"
    # public links are off by default: an anonymous reader can't open a deployment link
    as_people(monkeypatch)
    assert client.get(f"/api/shares/{token}").status_code == 401
    monkeypatch.undo()
    assert client.request("DELETE", f"/api/shares/{share['id']}", headers=ORIGIN,
                          json={"expected_revision": 1}).status_code == 204
    assert client.get(f"/api/shares/{token}").status_code == 404
    assert client.get("/api/shares/not-a-real-token-at-all").status_code == 404


def test_public_needs_policy_and_widening_needs_new_link(client, hub):
    cid, out = as_owner_conversation(client)
    denied = client.post("/api/shares/previews", headers=ORIGIN,
                         json={"conversation_id": cid, "audience": {"type": "public"}})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "public_disabled"
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    doc = svc.active().document
    doc["sharing"]["public_enabled"] = True
    svc.apply(doc, expected_revision=None, actor="t")
    pv = client.post("/api/shares/previews", headers=ORIGIN,
                     json={"conversation_id": cid, "audience": {"type": "deployment"}}).json()
    share = client.post("/api/shares", headers=ORIGIN, json={"preview_id": pv["preview_id"], "digest": pv["digest"]}).json()
    wider = client.patch(f"/api/shares/{share['id']}", headers=ORIGIN,
                         json={"expected_revision": 1, "audience": {"type": "public"}})
    assert wider.status_code == 422
    narrower = client.patch(f"/api/shares/{share['id']}", headers=ORIGIN,
                            json={"expected_revision": 1, "allow_copy": False})
    assert narrower.status_code == 200 and narrower.json()["allow_copy"] is False


def test_projection_strips_reasoning_and_tool_details(client):
    cid, b = new_conv(client)
    out = send(client, cid, "use a tool and think", b)
    wait_run(client, out["run_id"])
    pv = client.post("/api/shares/previews", headers=ORIGIN, json={"conversation_id": cid,
                                                                   "audience": {"type": "deployment"}}).json()
    reply = pv["projection"]["messages"][-1]
    kinds = {p["type"] for p in reply["content"]}
    assert "reasoning" not in kinds
    assert all(set(p) <= {"type", "toolCallId", "toolName", "result"} for p in reply["content"] if p["type"] == "tool-call")
    assert "tool arguments and results" in pv["omissions"]


def test_named_audience_and_copy_is_independent(client, hub, monkeypatch):
    from hubzoid.auth import users

    ana = users.find_by_email(hub, "ana@example.org")
    cid, out = as_owner_conversation(client)
    pv = client.post("/api/shares/previews", headers=ORIGIN, json={
        "conversation_id": cid, "audience": {"type": "named", "principals": [ana["id"]]}}).json()
    share = client.post("/api/shares", headers=ORIGIN, json={"preview_id": pv["preview_id"], "digest": pv["digest"]}).json()
    token = share["url"].rsplit("/", 1)[1]
    as_people(monkeypatch)
    assert client.get(f"/api/shares/{token}", headers=who("ben")).status_code == 404
    assert client.get(f"/api/shares/{token}", headers=who("ana")).status_code == 200
    cp = client.post(f"/api/shares/{token}/copies/previews", headers=who("ana")).json()
    made = client.post(f"/api/shares/{token}/copies", headers=who("ana"), json={"digest": cp["digest"]})
    assert made.status_code == 201
    new_cid = made.json()["new_conversation_id"]
    s = client.get(f"/api/conversations/{new_cid}", headers=who("ana")).json()
    assert s["runs"] == [] and s["queue"] == [] and len(s["messages"]) == 2
    client.app.state.chat.sharing.revoke(share["id"], "owner", None, manager=True)
    assert client.get(f"/api/shares/{token}", headers=who("ana")).status_code == 404
    assert client.get(f"/api/conversations/{new_cid}", headers=who("ana")).status_code == 200  # the copy stays


def test_collaborators_write_on_own_branch_and_lose_access(client, hub, monkeypatch):
    cid, out = as_owner_conversation(client)
    put = client.put(f"/api/conversations/{cid}/collaborators", headers=ORIGIN,
                     json={"expected_revision": 0, "members": [{"principal_id": "ana@example.org", "role": "editor"}]})
    assert put.status_code == 200 and put.json()["revision"] == 1
    stale = client.put(f"/api/conversations/{cid}/collaborators", headers=ORIGIN, json={"expected_revision": 0, "members": []})
    assert stale.status_code == 409
    as_people(monkeypatch)
    s = client.get(f"/api/conversations/{cid}", headers=who("ana")).json()
    assert s["role"] == "editor"
    # Ana writes on her own branch from the owner's answer.
    own = client.post(f"/api/conversations/{cid}/branches", headers=h(who("ana")),
                      json={"from_message_id": out["assistant_message_id"]}).json()["branch"]
    mine = send(client, cid, "my take", own, who("ana"), parent=out["assistant_message_id"])
    wait_run(client, mine["run_id"], who("ana"))
    # Queued work is admitted like a direct send: a suspended person can't queue.
    from hubzoid.access import store_for

    store_for(hub).suspend("ana@example.org", actor="t")
    queued = client.post(f"/api/conversations/{cid}/queue", headers=h(who("ana")),
                         json={"branch_id": own["id"], "content": "more"})
    assert queued.status_code == 403, queued.text
    store_for(hub).suspend("ana@example.org", actor="t", suspended=False)
    sharing = client.app.state.chat.sharing
    conv = {"id": cid, "owner_id": snap(client, cid, who("ana"))["conversation"]["owner_id"]}
    sharing.set_collaborators(conv, conv["owner_id"], [], 1)
    assert client.get(f"/api/conversations/{cid}", headers=who("ana")).status_code == 404


def test_deleting_a_shared_message_revokes_its_snapshot_links(client):
    cid, out = as_owner_conversation(client)
    pv = client.post("/api/shares/previews", headers=ORIGIN,
                     json={"conversation_id": cid, "head_message_id": out["assistant_message_id"]}).json()
    share = client.post("/api/shares", headers=ORIGIN, json={"preview_id": pv["preview_id"],
                                                               "digest": pv["digest"]}).json()
    token = share["url"].rsplit("/", 1)[1]
    rev = snap(client, cid)["conversation"]["graph_revision"]
    prev = client.post(f"/api/conversations/{cid}/deletion-preview", headers=ORIGIN,
                       json={"message_id": out["assistant_message_id"], "expected_revision": rev}).json()
    assert prev["snapshot_links"] == [share["id"]]
    done = client.post(f"/api/conversations/{cid}/operations", headers={**ORIGIN, "Idempotency-Key": "del-1"},
                       json={"kind": "delete", "target_message_id": out["assistant_message_id"],
                             "expected_revision": rev, "preview_hash": prev["preview_hash"]})
    assert done.status_code == 200 and done.json()["snapshot_links_revoked"] == [share["id"]]
    assert client.get(f"/api/shares/{token}").status_code == 404


def test_signed_download_links_never_travel_in_a_share():
    from hubzoid.chat.sharing import project, unlink

    text = ("Here it is.\n\n[Download report.md](https://hub.example.com/b/x/artifacts/web-c_1/report.md?t=abc&e=9)"
            "\nAlso https://hub.example.com/artifacts/web-c_1/my%20data.csv?e=9&t=def and "
            "[the docs](https://example.com/docs?t=1)")
    clean, names = unlink(text)
    assert names == ["report.md", "my data.csv"]
    for spelt in ("[r](/%61rtifacts/web-c/x.pdf?%74=TOK&e=1)", "<a href=\"/artifacts/web-c/x.pdf?e=1&amp;t=TOK\">r</a>",
                  "see https://h/b/x/artifacts/web-c/x.pdf?t=TOK."):
        out, found = unlink(spelt)
        assert "TOK" not in out and found == ["x.pdf"], spelt
    assert "t=abc" not in clean and "t=def" not in clean and "/artifacts/" not in clean
    assert "report.md" in clean and "[the docs](https://example.com/docs?t=1)" in clean
    msgs, omitted = project([{"id": "m1", "role": "assistant", "content": [{"type": "text", "text": text}]}],
                            {}, pseudonyms=False)
    assert "/artifacts/" not in msgs[0]["content"][0]["text"]
    assert {"file: report.md", "file: my data.csv"} <= set(omitted)


def _publish(client, cid, headers=ORIGIN, audience=None):
    pv = client.post("/api/shares/previews", headers=headers, json={
        "conversation_id": cid, "audience": audience or {"type": "deployment"}, "allow_copy": True}).json()
    made = client.post("/api/shares", headers=headers, json={"preview_id": pv["preview_id"], "digest": pv["digest"]})
    assert made.status_code == 201, made.text
    return made.json()


def test_links_stored_by_older_builds_are_stripped_on_read_and_copy(client, hub, monkeypatch):
    import json

    import sqlalchemy as sa

    from hubzoid.chat.sharing import manifests

    cid, _ = as_owner_conversation(client)
    share = _publish(client, cid)
    token = share["url"].rsplit("/", 1)[1]
    svc = client.app.state.chat.sharing
    with svc.engine.begin() as conn:
        row = conn.execute(sa.select(manifests.c.projection_json).where(manifests.c.id == share["id"])).first()
        p = json.loads(row.projection_json)
        p["messages"][-1]["content"].append({"type": "text", "text": "[Download x.pdf](/artifacts/web-c/x.pdf?t=zz)"})
        conn.execute(manifests.update().where(manifests.c.id == share["id"]).values(projection_json=json.dumps(p)))
    body = client.get(f"/api/shares/{token}").json()
    assert "t=zz" not in json.dumps(body) and "file: x.pdf" in body["omissions"]
    as_people(monkeypatch)
    cp = client.post(f"/api/shares/{token}/copies/previews", headers=who("ana")).json()
    made = client.post(f"/api/shares/{token}/copies", headers=who("ana"), json={"digest": cp["digest"]}).json()
    copied = client.get(f"/api/conversations/{made['new_conversation_id']}", headers=who("ana")).json()
    assert "t=zz" not in json.dumps(copied["messages"])


@pytest.mark.parametrize("how", ["delete", "block"])
def test_an_owner_who_is_deleted_or_blocked_stops_their_links(client, hub, monkeypatch, how):
    import sqlalchemy as sa

    from hubzoid.auth import users
    from hubzoid.chat.sharing import manifests

    as_people(monkeypatch)
    ana = who("ana")
    cid, b = new_conv(client, ana)
    wait_run(client, send(client, cid, "the plan", b, ana)["run_id"], ana)
    share = _publish(client, cid, ana)
    token = share["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/shares/{token}", headers=who("ben")).status_code == 200
    owner = users.find_by_email(hub, "ana@example.org")
    if how == "delete":
        users.store(hub).delete(owner["id"])
        with client.app.state.chat.sharing.engine.connect() as conn:
            assert conn.execute(sa.select(manifests.c.status).where(manifests.c.id == share["id"])).scalar() == "revoked"
    else:
        from hubzoid.access import store_for
        store_for(hub).suspend("ana@example.org", actor="admin@example.org")
    assert client.get(f"/api/shares/{token}", headers=who("ben")).status_code == 404
