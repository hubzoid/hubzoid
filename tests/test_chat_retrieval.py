"""Sources, retrieval, citations and safe fetching (HUB-75)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, PEOPLE, as_people, build_app, make_hub, who
from tests.test_chat_operations import h, new_conv, op, wait_run


@pytest.fixture
def hub(tmp_path, monkeypatch):
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


POLICY = ("# Order exceptions\n\nAn order exception above 500 units must be escalated to the operations lead "
          "within one working day.\n\nSmall exceptions are recorded in the weekly log.")


def put(client, cid, manifest, rev, headers=ORIGIN):
    return client.put(f"/api/chat/retrieval/collections/{cid}", headers=headers,
                      json={"expected_revision": rev, "manifest": {"title": cid.title(), **manifest}})


def sync(client, cid, headers=ORIGIN):
    svc = client.app.state.chat.retrieval
    actor = client.app.state.chat.engine.actor(
        PEOPLE[headers["X-Test-User"]] if "X-Test-User" in headers else _owner(client))
    return svc.start_sync(actor, cid, background=False)


def _owner(client):
    from hubzoid import auth

    return auth.local_owner(client.app.state.chat.hub_dir)


def test_collection_sync_query_and_citation(client, monkeypatch):
    made = put(client, "policies", {"sources": [{"id": "rules", "kind": "text", "text": POLICY, "title": "Rules"}]},
               "absent")
    assert made.status_code == 200 and made.json()["revision"] == 1
    assert put(client, "policies", {"sources": []}, "absent").status_code == 409
    job = sync(client, "policies")
    assert job["state"] == "ready" and job["counters"]["passages"] >= 1
    q = client.post("/api/chat/retrieval/queries", headers=ORIGIN,
                    json={"query": "When must an order exception be escalated?"}).json()
    assert q["results"] and "operations lead" in q["results"][0]["excerpt"]
    cit = client.get(f"/api/chat/retrieval/citations/{q['results'][0]['citation_id']}").json()
    assert cit["title"] == "Rules" and "500 units" in cit["passage"]
    assert client.get("/api/chat/retrieval/citations/not-a-citation").status_code == 404
    # Someone who isn't a reader sees nothing, not even that it exists.
    as_people(monkeypatch)
    assert client.get("/api/chat/retrieval/collections", headers=who("ana")).json()["items"] == []
    other = client.post("/api/chat/retrieval/queries", headers=who("ana"), json={"query": "escalated"}).json()
    assert other["results"] == [] and other["collections"] == []
    assert client.get(f"/api/chat/retrieval/citations/{q['results'][0]['citation_id']}",
                      headers=who("ana")).status_code == 404


def test_removed_source_leaves_results_at_once_and_partial_sync_keeps_the_rest(client, monkeypatch):
    svc = client.app.state.chat.retrieval
    pages = {"ok": True}

    def fake_fetch(url, limits, allowed=None, blocked=None):
        if not pages["ok"]:
            from hubzoid.chat.graph import GraphError

            raise GraphError(502, "provider_failed", "down")
        return url, "text/html", b"<html><title>Supplier page</title><body><p>Lead time is nine days.</p></body></html>"

    monkeypatch.setattr(svc, "fetch", fake_fetch)
    blocked = put(client, "mixed", {"sources": [{"id": "s", "kind": "url", "url": "https://supplier.example/terms"}]},
                  "absent")
    assert blocked.status_code == 403  # web fetching is off for the deployment by default
    _web_on(client.app.state.chat.hub_dir)
    put(client, "mixed", {"sources": [{"id": "rules", "kind": "text", "text": POLICY},
                                      {"id": "supplier", "kind": "url", "url": "https://supplier.example/terms"}]},
        "absent")
    assert sync(client, "mixed")["state"] == "ready"
    q = lambda text: client.post("/api/chat/retrieval/queries", headers=ORIGIN, json={"query": text}).json()["results"]
    assert q("lead time nine days")[0]["title"] == "Supplier page"
    pages["ok"] = False
    again = sync(client, "mixed")
    assert again["state"] == "ready" and again["counters"]["failed"] == 1
    assert q("lead time nine days"), "a failed read keeps the source's previous passages"
    cited = q("escalated operations")[0]["citation_id"]
    assert put(client, "mixed", {"sources": [{"id": "supplier", "kind": "url", "url": "https://supplier.example/terms"}]},
               1).status_code == 200
    assert not any(r["title"] == "rules" for r in q("escalated operations")), "removed before any re-sync"
    assert client.get(f"/api/chat/retrieval/citations/{cited}").status_code == 404


def _web_on(hub):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    doc = svc.active().document
    doc["retrieval"]["web"]["enabled"] = True
    __import__('tests.chat_helpers', fromlist=['x']).write_chat_config(svc.hub_dir, doc)


def test_changed_and_directory_sources_and_file_use(client, hub):
    (hub / "manuals").mkdir()
    (hub / "manuals" / "returns.md").write_text("Returns are accepted within 30 days.")
    put(client, "docs", {"sources": [{"id": "rules", "kind": "text", "text": POLICY},
                                     {"id": "manuals", "kind": "directory", "path": "manuals"}]}, "absent")
    sync(client, "docs")
    q = lambda text: client.post("/api/chat/retrieval/queries", headers=ORIGIN, json={"query": text}).json()["results"]
    assert q("returns accepted days") and q("escalated operations lead")
    # Adding a reader keeps the folder's files; changing a source's definition hides it until the next sync.
    put(client, "docs", {"readers": ["*"], "sources": [{"id": "rules", "kind": "text", "text": "Something else entirely."},
                                                       {"id": "manuals", "kind": "directory", "path": "manuals"}]}, 1)
    assert q("returns accepted days"), "a directory's files survive a manifest edit"
    assert not q("escalated operations lead"), "a changed source leaves results at once"
    # An uploaded file used by a collection can't be deleted from under it.
    cid, _ = new_conv(client)
    fid = client.post(f"/api/conversations/{cid}/files", headers=ORIGIN,
                      files={"file": ("h.md", b"Pallets are wrapped twice.", "text/markdown")}).json()["id"]
    put(client, "docs", {"readers": ["*"], "sources": [{"id": "h", "kind": "upload", "file_id": fid}]}, 2)
    refs = client.get(f"/api/chat/files/{fid}/references").json()["items"]
    for r in refs:
        if r["conversation_id"]:
            client.delete(f"/api/chat/files/{fid}/references/{r['reference_id']}", headers=ORIGIN)
    assert client.request("DELETE", f"/api/chat/files/{fid}", headers=ORIGIN, json={}).status_code == 409


def test_readers_and_upload_sources(client, monkeypatch):
    cid, _ = new_conv(client)
    up = client.post(f"/api/conversations/{cid}/files", headers=ORIGIN,
                     files={"file": ("handbook.md", b"Returns are accepted within 30 days of delivery.", "text/markdown")})
    fid = up.json()["id"]
    bad = put(client, "team", {"sources": [{"id": "hb", "kind": "upload", "file_id": "nope"}]}, "absent")
    assert bad.status_code == 404
    assert put(client, "team", {"readers": ["ana@example.org"], "sources": [
        {"id": "hb", "kind": "upload", "file_id": fid}]}, "absent").status_code == 200
    assert sync(client, "team")["state"] == "ready"
    as_people(monkeypatch)
    got = client.post("/api/chat/retrieval/queries", headers=who("ana"), json={"query": "returns accepted days"}).json()
    assert got["results"] and got["results"][0]["title"] == "handbook.md"
    assert client.get("/api/chat/retrieval/collections", headers=who("ben")).json()["items"] == []
    denied = put(client, "team", {"sources": []}, 1, headers=h(who("ana")))
    assert denied.status_code == 403  # a reader can't change it


def test_safe_fetch_refuses_unsafe_addresses(monkeypatch):
    from hubzoid.chat import feedback as fb
    from hubzoid.chat.graph import GraphError
    from hubzoid.chat.retrieval import safe_fetch

    lim = {"redirects": 2, "bytes": 1000, "timeout_seconds": 5}
    for url in ("http://example.com/", "https://user:pw@example.com/", "https://example.com:8443/", "ftp://x/"):
        with pytest.raises(GraphError) as exc:
            safe_fetch(url, lim)
        assert exc.value.code == "unsafe_url", url
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("169.254.169.254", 443))])
    with pytest.raises(GraphError) as exc:
        safe_fetch("https://metadata.example/", lim)
    assert exc.value.code == "unsafe_url"
    with pytest.raises(GraphError):
        safe_fetch("https://x.blocked.example/", lim, blocked=["blocked.example"])
    with pytest.raises(GraphError):
        safe_fetch("https://elsewhere.example/", lim, allowed=["only.example"])


def test_search_sources_tool_cites_for_the_signed_in_person(client, hub):
    import asyncio
    import json

    from hubzoid import access
    from hubzoid.tools import sources

    put(client, "policies", {"sources": [{"id": "rules", "kind": "text", "text": POLICY}]}, "absent")
    sync(client, "policies")
    tool = sources.make(type("Ctx", (), {"hub_dir": hub})())[0]
    owner = _owner(client)

    from agents.tool_context import ToolContext

    async def call(identity):
        args = json.dumps({"query": "escalated exception"})
        with access.identity_scope(identity):
            return await tool.on_invoke_tool(
                ToolContext(context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args), args)

    out = asyncio.run(call(access.Identity.make(user=owner.email, surface="web")))
    assert "[cite:" in out and "operations lead" in out
    assert "signed-in" in asyncio.run(call(access.Identity()))


def test_web_search_turn_needs_policy_and_grant(client, hub, monkeypatch):
    from hubzoid import _request_ctx
    from hubzoid.chat import config as cfg

    seen = []
    runtime = client.app.state.chat.runtime
    original = runtime.stream_events

    def spy(prompt):
        slot = _request_ctx.native_session()
        seen.append(bool(slot and slot.web_search))
        return original(prompt)

    monkeypatch.setattr(runtime, "stream_events", spy)
    cid, b = new_conv(client)

    def ask(branch, parent=None):
        out = op(client, cid, {"kind": "send", "branch_id": branch["id"], "parent_id": parent,
                               "expected_revision": branch["revision"], "content": "news?", "web_search": True}).json()
        wait_run(client, out["run_id"])
        return out

    first = ask(b)
    assert seen[-1] is False  # off by default for the deployment
    svc = cfg.service_for(hub)
    doc = svc.active().document
    doc["retrieval"]["web"]["enabled"] = True
    __import__('tests.chat_helpers', fromlist=['x']).write_chat_config(svc.hub_dir, doc)
    b2 = client.get(f"/api/conversations/{cid}/branches", headers=ORIGIN).json()["items"][0]
    ask(b2, first["assistant_message_id"])
    assert seen[-1] is True  # the local owner holds every grant
