"""Folders, pins, tags, bookmarks, search and import/export (HUB-73)."""
from __future__ import annotations

import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who
from tests.test_chat_operations import h, new_conv, op, send, snap, wait_run


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def chat(client, text, headers=ORIGIN):
    cid, b = new_conv(client, headers)
    out = send(client, cid, text, b, headers)
    wait_run(client, out["run_id"], headers)
    return cid, out


def reindexed(client, *cids):
    for cid in cids:
        client.app.state.chat.search.reindex(cid)


# -- organisation -----------------------------------------------------------------------
def test_folder_defaults_inherit_replace_clear_and_merge(client):
    root = client.post("/api/chat/folders", headers=ORIGIN, json={"name": "Operations", "defaults": {
        "prompt_ids": ["p1"], "model_ids": ["team-default"], "parameters": {"temperature": 0.2, "top_p": 0.9}}}).json()
    child = client.post("/api/chat/folders", headers=ORIGIN, json={"name": "Weekly", "parent_id": root["id"],
                                                                    "defaults": {"prompt_ids": [], "parameters": {
                                                                        "top_p": None, "max_tokens": 500}}}).json()
    ctx = client.get(f"/api/chat/folders/{child['id']}/effective-context").json()
    assert ctx["effective"]["prompt_ids"] == [] and ctx["effective"]["model_ids"] == ["team-default"]
    assert ctx["effective"]["parameters"] == {"temperature": 0.2, "max_tokens": 500}
    assert ctx["provenance"]["model_ids"] == root["id"] and ctx["provenance"]["prompt_ids"] == child["id"]
    cycle = client.patch(f"/api/chat/folders/{root['id']}", headers=ORIGIN,
                         json={"expected_revision": 1, "parent_id": child["id"]})
    assert cycle.status_code == 409
    dup = client.post("/api/chat/folders", headers=ORIGIN, json={"name": "operations"})
    assert dup.status_code == 409


def test_placement_is_personal_and_bulk_is_all_or_nothing(client, monkeypatch):
    cid, _ = chat(client, "budget review")
    other_cid, _ = chat(client, "rota")
    folder = client.post("/api/chat/folders", headers=ORIGIN, json={"name": "Finance"}).json()
    tag = client.post("/api/chat/tags", headers=ORIGIN, json={"name": "recurring", "color": "saffron"}).json()
    r = client.patch(f"/api/conversations/{cid}/organization", headers=ORIGIN,
                     json={"expected_revision": 0, "folder_id": folder["id"], "pinned_position": 1,
                           "tag_ids": [tag["id"]]})
    assert r.status_code == 200 and r.json()["tag_ids"] == [tag["id"]]
    bad = client.post("/api/chat/organization/bulk", headers=ORIGIN, json={
        "action": "archive", "items": [{"id": other_cid, "expected_revision": 0}, {"id": "c_nope000000x"}]})
    assert bad.status_code == 404 and bad.json()["detail"]["code"] == "resource_not_found"
    org = client.get("/api/chat/organization").json()
    assert not any(p["conversation_id"] == other_cid and p["archived"] for p in org["placements"])
    ok = client.post("/api/chat/organization/bulk", headers=ORIGIN, json={
        "action": "archive", "items": [{"id": other_cid, "expected_revision": 0}]})
    assert ok.status_code == 200 and ok.json()["affected"] == 1
    as_people(monkeypatch)
    assert client.get("/api/chat/organization", headers=who("ana")).json()["placements"] == []


def test_manifest_dry_run_then_apply_digest(client):
    cid, _ = chat(client, "weekly stock")
    manifest = {"version": 1, "scope": "self", "expected_revision": 0,
                "folders": [{"id": "11111111-1111-4111-8111-111111111111", "name": "Operations", "parent_id": None,
                             "position": 0, "defaults": {}}],
                "tags": [{"id": "33333333-3333-4333-8333-333333333333", "name": "recurring", "color": "saffron"}],
                "placements": [{"conversation_id": cid, "folder_id": "11111111-1111-4111-8111-111111111111",
                                "archived": False, "tag_ids": ["33333333-3333-4333-8333-333333333333"]}]}
    dry = client.post("/api/chat/organization/apply", headers=ORIGIN, json={"manifest": manifest, "dry_run": True}).json()
    assert dry["diff"]["create_folders"] == 1 and dry["digest"].startswith("sha256:")
    wrong = client.post("/api/chat/organization/apply", headers=ORIGIN,
                        json={"manifest": manifest, "dry_run": False, "digest": "sha256:00"})
    assert wrong.status_code == 409
    done = client.post("/api/chat/organization/apply", headers=ORIGIN,
                       json={"manifest": manifest, "dry_run": False, "digest": dry["digest"]}).json()
    assert done["applied"]
    org = client.get("/api/chat/organization").json()
    assert org["placements"][0]["folder_id"] == "11111111-1111-4111-8111-111111111111"


# -- search ---------------------------------------------------------------------------------
def test_search_matches_and_respects_access(client, monkeypatch):
    mine, _ = chat(client, "quarterly budget forecast")
    reindexed(client, mine)
    found = client.get("/api/chat/search", params={"q": "budget"}).json()["items"]
    assert any(i["open_target"]["conversation_id"] == mine and i["kind"] == "message" for i in found)
    assert client.get("/api/chat/search", params={"q": "quart"}).json()["items"]  # prefix on the last word
    assert client.get("/api/chat/search", params={"q": '"budget forecast"'}).json()["items"]
    assert not client.get("/api/chat/search", params={"q": '"forecast budget"'}).json()["items"]
    assert client.get("/api/chat/search", params={"q": '"open'}).status_code == 400
    assert client.get("/api/chat/search", params={"q": "budget OR *"}).status_code == 200  # no operators
    as_people(monkeypatch)
    theirs = client.get("/api/chat/search", params={"q": "budget"}, headers=who("ana")).json()["items"]
    assert theirs == []  # someone else's conversation never shows, not even as a count
    monkeypatch.undo()


def test_deleted_conversation_vanishes_from_search_at_once(client):
    cid, _ = chat(client, "secret merger notes")
    reindexed(client, cid)
    assert client.get("/api/chat/search", params={"q": "merger"}).json()["items"]
    client.request("DELETE", f"/api/conversations/{cid}", headers=ORIGIN, json={})
    assert client.get("/api/chat/search", params={"q": "merger"}).json()["items"] == []


# -- portability ------------------------------------------------------------------------------
def _preview(client, data: bytes, headers=ORIGIN):
    return client.post("/api/chat/imports/preview", headers=headers, files={"file": ("x.zip", data, "application/zip")})


def test_portable_graph_roundtrip(client):
    cid, out = chat(client, "first")
    regen = op(client, cid, {"kind": "regenerate", "target_message_id": out["user_message_id"]}).json()
    wait_run(client, regen["run_id"])
    exp = client.post("/api/chat/exports", headers=ORIGIN, json={"resource_ids": [cid]}).json()
    data = client.get(exp["download_url"]).content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert sorted(n for n in zf.namelist() if not n.startswith("blobs/")) == ["manifest.json", "records.jsonl"]
        records = [json.loads(l) for l in zf.read("records.jsonl").decode().splitlines()]
    assert sum(1 for r in records if r["type"] == "message") == 3  # both answers travel
    assert not any("grant" in json.dumps(r) or "run_id" in json.dumps(r) for r in records)
    pv = _preview(client, data).json()
    assert all(o["disposition"] == "import" for o in pv["objects"])
    applied = client.post("/api/chat/imports", headers=ORIGIN, json={"preview_id": pv["id"], "digest": pv["digest"]})
    assert applied.status_code == 201, applied.text
    new_cid = next(iter(applied.json()["conversations"].values()))
    s = snap(client, new_cid)
    assert len(s["messages"]) == 3 and not s["runs"] and len(s["branches"]) == 2
    again = client.post("/api/chat/imports", headers=ORIGIN, json={"preview_id": pv["id"], "digest": pv["digest"]})
    assert again.status_code == 200 and again.json() == applied.json()  # test_portable_import_idempotent_restart


def test_portable_zip_traversal_and_corrupt_blob_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    assert _preview(client, buf.getvalue()).status_code == 422
    body = b'{"version":1,"type":"conversation","source_id":"c1","data":{},"refs":{}}'
    import hashlib

    manifest = {"version": 1, "format": "hubzoid-exchange", "records_sha256": hashlib.sha256(body).hexdigest(),
                "record_count": 1, "blobs": [{"sha256": "0" * 64, "size": 1}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("records.jsonl", body)
        zf.writestr("blobs/" + "0" * 64, b"not that hash")
    pv = _preview(client, buf.getvalue()).json()
    assert any(o["reason"] == "hash_mismatch" and o["disposition"] == "reject" for o in pv["objects"])


def test_openwebui_and_librechat_imports(client):
    owui = [{"id": "o1", "title": "From OWUI", "chat": {"history": {"currentId": "b", "messages": {
        "a": {"id": "a", "parentId": None, "role": "user", "content": "hi"},
        "b": {"id": "b", "parentId": "a", "role": "assistant", "content": "hello"}}}}}]
    pv = client.post("/api/chat/imports/preview", headers=ORIGIN,
                     files={"file": ("owui.json", json.dumps(owui).encode(), "application/json")}).json()
    assert pv["format"] == "openwebui"
    out = client.post("/api/chat/imports", headers=ORIGIN, json={"preview_id": pv["id"], "digest": pv["digest"]}).json()
    assert out["messages"] == 2
    lc = {"conversationId": "l1", "title": "From LibreChat", "messages": [
        {"messageId": "x", "parentMessageId": "00000000-0000-0000-0000-000000000000", "text": "q", "isCreatedByUser": True},
        {"messageId": "y", "parentMessageId": "x", "text": "a", "isCreatedByUser": False},
        {"messageId": "z", "parentMessageId": "missing", "text": "orphan", "isCreatedByUser": False}]}
    pv = client.post("/api/chat/imports/preview", headers=ORIGIN,
                     files={"file": ("lc.json", json.dumps(lc).encode(), "application/json")}).json()
    assert {o["source_id"]: o["disposition"] for o in pv["objects"] if o["type"] == "message"}["z"] == "reject"
    forced = client.post("/api/chat/imports", headers=ORIGIN,
                         json={"preview_id": pv["id"], "digest": pv["digest"], "dispositions": {"message:z": "import"}})
    assert forced.status_code == 422


def test_portable_cross_user_denied(client, monkeypatch):
    cid, _ = chat(client, "mine")
    exp = client.post("/api/chat/exports", headers=ORIGIN, json={"resource_ids": [cid]}).json()
    pv = _preview(client, client.get(exp["download_url"]).content).json()
    as_people(monkeypatch)
    assert client.get(exp["download_url"], headers=who("ana")).status_code == 404
    assert client.post("/api/chat/imports", headers=who("ana"),
                       json={"preview_id": pv["id"], "digest": pv["digest"]}).status_code == 404
    denied = client.post("/api/chat/exports", headers=who("ana"), json={"resource_ids": [cid]})
    assert denied.status_code == 404
