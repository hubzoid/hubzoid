"""Files with lasting identity, references and artifacts (HUB-74) through the API."""
from __future__ import annotations

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, PEOPLE, as_people, build_app, make_hub, who
from tests.test_chat_operations import h, new_conv


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


def upload(client, data: bytes, name="orders.csv", mime="text/csv", conversation=None, headers=ORIGIN):
    """One request (POST /api/chat/files), attached to ``conversation`` when given."""
    r = client.post("/api/chat/files", headers=h(headers), files={"file": (name, data, mime)})
    assert r.status_code == 201, r.text
    d = r.json()
    if conversation:
        att = client.post(f"/api/conversations/{conversation}/files/reuse", headers=h(headers),
                          json={"file_id": d["file_id"]})
        assert att.status_code == 201, att.text
    return ready(client, d["file_id"], headers)


def ready(client, file_id, headers=ORIGIN):
    end = time.time() + 10
    while time.time() < end:
        d = client.get(f"/api/chat/files/{file_id}", headers=headers).json()
        if d["state"] != "validating":
            return d
        time.sleep(0.05)
    raise AssertionError("still validating")


def test_one_request_upload_ranges_and_preview(client):
    data = b"id,qty\n" + b"1,2\n" * 50
    d = upload(client, data)
    assert d["state"] == "ready" and d["content_sha256"] == hashlib.sha256(data).hexdigest()
    part = client.get(f"/api/chat/files/{d['file_id']}/content", headers={"Range": "bytes=0-5"})
    assert part.status_code == 206 and part.content == data[:6] and part.headers["content-range"].endswith(f"/{len(data)}")
    full = client.get(f"/api/chat/files/{d['file_id']}/content")
    assert full.content == data and full.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in full.headers["content-disposition"]
    assert "qty" in client.get(f"/api/chat/files/{d['file_id']}/preview").json()["text"]


def test_types_limits_and_refused_bytes(client):
    r = client.post("/api/chat/files", headers=h(), files={"file": (
        "macro.xlsm", b"PK\x03\x04", "application/vnd.ms-excel.sheet.macroEnabled.12")})
    assert r.status_code == 415
    r = client.post("/api/chat/files", headers={**h(), "Content-Length": str(10**9)}, content=b"x")
    assert r.status_code == 413
    r = client.post("/api/chat/files", headers=h(), files={"file": (
        "pic.png", b"<html><script>alert(1)</script></html>", "image/png")})
    assert r.status_code == 415 and r.json()["detail"]["code"] == "type_mismatch"


def test_reuse_follows_conversation_access_and_delete_needs_no_references(client, monkeypatch):
    cid, _ = new_conv(client)
    other, _ = new_conv(client)
    d = upload(client, b"a,b\n1,2\n", conversation=cid)
    att = client.post(f"/api/conversations/{other}/files/reuse", headers=h(), json={"file_id": d["file_id"]})
    assert att.status_code == 201 and att.json()["id"] == d["file_id"] and att.json()["name"].startswith("orders")
    refs = client.get(f"/api/chat/files/{d['file_id']}/references").json()["items"]
    assert {r["conversation_id"] for r in refs} == {cid, other}
    as_people(monkeypatch)
    assert client.get(f"/api/chat/files/{d['file_id']}", headers=who("ana")).status_code == 404
    # Shared with Ana as a viewer: she can read the file through that conversation, not reuse-write.
    conv = client.app.state.chat.store.get_conversation(cid)
    client.app.state.chat.sharing.set_collaborators(conv, conv["owner_id"],
                                                    [{"email": "ana@example.org", "role": "viewer"}], 0)
    assert client.get(f"/api/chat/files/{d['file_id']}", headers=who("ana")).status_code == 200
    assert client.post(f"/api/conversations/{cid}/files/reuse", headers=h(who("ana")),
                       json={"file_id": d["file_id"]}).status_code == 404  # viewer can't write there
    monkeypatch.undo()
    busy = client.request("DELETE", f"/api/chat/files/{d['file_id']}", headers=ORIGIN, json={})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "file_in_use"
    for r in refs:
        assert client.delete(f"/api/chat/files/{d['file_id']}/references/{r['reference_id']}",
                             headers=ORIGIN).status_code == 204
    gone = client.request("DELETE", f"/api/chat/files/{d['file_id']}", headers=ORIGIN, json={})
    assert gone.status_code == 202 and gone.json()["state"] == "deleted"
    assert client.get(f"/api/chat/files/{d['file_id']}/content").status_code == 404


def test_the_upload_id_is_the_file_id_and_attaching_references_it(client):
    cid, _ = new_conv(client)
    d = upload(client, b"hello", name="notes.txt", mime="text/plain", conversation=cid)
    assert client.get(f"/api/chat/files/{d['file_id']}/content").content == b"hello"
    items = client.get("/api/chat/files", params={"conversation_id": cid}).json()["items"]
    assert [f["file_id"] for f in items] == [d["file_id"]]


def test_a_file_deleted_from_the_store_stays_readable_in_old_chats(client):
    from hubzoid import memory as memlib
    from hubzoid.chat.store import chat_key

    cid, _ = new_conv(client)
    d = upload(client, b"sku,qty\nA1,4\n", conversation=cid)
    folder = memlib.chat_upload_dir(client.app.state.chat.hub_dir,
                                    chat_key(client.app.state.chat.store.get_conversation(cid)))
    for r in client.get(f"/api/chat/files/{d['file_id']}/references").json()["items"]:
        client.delete(f"/api/chat/files/{d['file_id']}/references/{r['reference_id']}", headers=ORIGIN)
    gone = client.request("DELETE", f"/api/chat/files/{d['file_id']}", headers=ORIGIN, json={})
    assert gone.status_code == 202
    assert client.get(f"/api/chat/files/{d['file_id']}/content").status_code == 404
    assert (folder / "orders.csv").read_bytes() == b"sku,qty\nA1,4\n"   # the chat's own link keeps the bytes


def test_artifacts_capture_versions_and_conflicting_edit(client):
    from hubzoid import memory as memlib
    from hubzoid.chat.store import chat_key

    cid, b = new_conv(client)
    chat = client.app.state.chat
    conv = chat.store.get_conversation(cid)
    folder = memlib.chat_artifact_dir(chat.hub_dir, chat_key(conv))
    (folder / "report.html").write_text("<h1>Orders</h1>")
    owner = conv["owner_id"]
    t0 = time.time() - 5
    made = chat.files.capture_artifacts(conversation_id=cid, branch_id=b["id"], actor_id=owner, run_id="r1",
                                        message_id="m1", since=t0)
    assert len(made) == 1 and made[0]["kind"] == "html" and made[0]["versions"][0]["provenance"] == "generated"
    assert chat.files.capture_artifacts(conversation_id=cid, branch_id=b["id"], actor_id=owner, run_id="r2",
                                        message_id="m2", since=t0) == []  # unchanged bytes: no new version
    art = client.get("/api/chat/artifacts", params={"conversation_id": cid}).json()["items"][0]
    v1 = art["versions"][0]["version_id"]
    src = client.get(f"/api/chat/artifacts/{art['artifact_id']}/versions/{v1}/content", params={"as": "text"}).json()
    assert src["text"] == "<h1>Orders</h1>"
    raw = client.get(f"/api/chat/artifacts/{art['artifact_id']}/versions/{v1}/content")
    assert raw.headers["content-type"] == "application/octet-stream"  # never served as a page
    stale = client.post(f"/api/chat/artifacts/{art['artifact_id']}/versions", headers=ORIGIN,
                        json={"content": "<h1>x</h1>", "base_version_id": v1, "expected_revision": 0})
    assert stale.status_code == 409
    ok = client.post(f"/api/chat/artifacts/{art['artifact_id']}/versions", headers=ORIGIN,
                     json={"content": "<h1>Orders (fixed)</h1>", "base_version_id": v1, "expected_revision": 1})
    assert ok.status_code == 201
    vers = ok.json()["versions"]
    assert [v["provenance"] for v in vers] == ["generated", "edited"] and vers[1]["base_version_id"] == v1


def test_artifact_frame_is_locked_down(client):
    r = client.get("/api/chat/artifact-frame")
    csp = r.headers["content-security-policy"]
    assert "frame-src blob:" in csp and "default-src 'none'" in csp
    from hubzoid.chat.file_routes import CHILD_CSP

    assert json.dumps(CHILD_CSP) in r.text  # the child's policy is a JSON string inside the script and "connect-src 'none'" in r.text and 'sandbox="allow-scripts"' in r.text


def test_storage_is_local_and_s3_files_fail_one_by_one(client, tmp_path):
    import uuid

    from pydantic import ValidationError

    from hubzoid.chat import storage as st
    from hubzoid.chat.file_service import FilesSettings, files, storage_configs

    # Older settings documents still validate; nothing writes to S3 any more.
    with pytest.raises(ValidationError):
        FilesSettings.model_validate({"storage": {"adapter": "local", "bucket": "b"}})
    FilesSettings.model_validate({"storage": {"adapter": "s3", "bucket": "b", "credential_ref": "env:HZ_S3"}})
    with pytest.raises(st.StorageError):
        st.for_settings(tmp_path, {"adapter": "s3", "bucket": "b"})
    local = st.LocalStorage(tmp_path)
    local.put("x/y", b"z")
    assert local.read("x/y") == b"z"
    with pytest.raises(st.StorageError):
        local.put("../escape", b"no")
    # A file an older release stored in S3: that file answers 503, the hub keeps working.
    svc = client.app.state.chat.files
    d = upload(client, b"a,b\n")
    with svc.engine.begin() as conn:
        conn.execute(storage_configs.insert().values(id="sc_olds3", adapter="s3", created_at=0,
                                                     config_json=json.dumps({"adapter": "s3", "bucket": "b"})))
        fid = str(uuid.uuid4())
        row = dict(conn.execute(files.select().where(files.c.file_id == d["file_id"])).first()._mapping)
        conn.execute(files.insert().values({**row, "file_id": fid, "storage_config_id": "sc_olds3"}))
    r = client.get(f"/api/chat/files/{fid}/content")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "storage_unavailable"
    assert client.get(f"/api/chat/files/{d['file_id']}/content").content == b"a,b\n"
    assert svc.unreadable_files() == 1
    assert any(c["check"] == "s3_files" and not c["ok"] for c in svc.doctor(write_probe=False)["checks"])


def test_restart_recovers_files_left_validating_and_syncs_left_running(client):
    from hubzoid.chat.file_service import files
    from hubzoid.chat.retrieval import jobs

    svc = client.app.state.chat.files
    d = upload(client, b"sku,qty\nA1,4\n")
    with svc.engine.begin() as conn:  # as if the process stopped mid-check
        conn.execute(files.update().where(files.c.file_id == d["file_id"]).values(state="validating", created_at=0))
        conn.execute(jobs.insert().values(id="j-stuck", hub_id=client.app.state.chat.retrieval.hub,
                                          collection_id="c", operation="sync", principal_id="p", state="indexing",
                                          counters_json="{}", created_at=0, updated_at=0))
    assert svc.recover() == 1
    assert client.get(f"/api/chat/files/{d['file_id']}", headers=ORIGIN).json()["state"] == "ready"
    assert client.app.state.chat.retrieval.recover() == 1
    with svc.engine.connect() as conn:
        assert conn.execute(jobs.select().where(jobs.c.id == "j-stuck")).first().error_code == "interrupted"


def test_someone_elses_reuse_never_locks_the_owner_out(client, monkeypatch):
    cid, _ = new_conv(client)
    d = upload(client, b"secret,1\n", conversation=cid)
    conv = client.app.state.chat.store.get_conversation(cid)
    sharing = client.app.state.chat.sharing
    sharing.set_collaborators(conv, conv["owner_id"], [{"email": "ana@example.org", "role": "viewer"}], 0)
    as_people(monkeypatch)
    ana = who("ana")
    mine, _ = new_conv(client, ana)
    assert client.post(f"/api/conversations/{mine}/files/reuse", headers=h(ana),
                       json={"file_id": d["file_id"]}).status_code == 201
    sharing.set_collaborators(conv, conv["owner_id"], [], 1)  # Ana removed from the chat
    assert client.get(f"/api/chat/files/{d['file_id']}", headers=ana).status_code == 200  # via her reuse
    monkeypatch.undo()
    refs = client.get(f"/api/chat/files/{d['file_id']}/references").json()["items"]
    assert [r.get("elsewhere") for r in refs].count(True) == 1  # the owner sees it, not where
    assert all(r["conversation_id"] in (cid, None) for r in refs)
    own = next(r for r in refs if r["conversation_id"] == cid)
    client.delete(f"/api/chat/files/{d['file_id']}/references/{own['reference_id']}", headers=ORIGIN)
    gone = client.request("DELETE", f"/api/chat/files/{d['file_id']}", headers=ORIGIN, json={})
    assert gone.status_code == 202, gone.text
    as_people(monkeypatch)
    assert client.get(f"/api/chat/files/{d['file_id']}", headers=ana).status_code == 404


def test_a_viewer_attaches_nothing_and_other_image_types_are_files(client, monkeypatch):
    cid, _ = new_conv(client)
    conv = client.app.state.chat.store.get_conversation(cid)
    client.app.state.chat.sharing.set_collaborators(conv, conv["owner_id"],
                                                    [{"email": "ana@example.org", "role": "viewer"}], 0)
    as_people(monkeypatch)
    ana = who("ana")
    tiff = client.post("/api/chat/files", headers=h(ana), files={"file": ("scan.tiff", b"II*\x00rest", "image/tiff")})
    assert tiff.status_code == 201   # not a picture the models take: kept as a file, not refused
    r = client.post(f"/api/conversations/{cid}/operations", headers={**h(ana), "Idempotency-Key": "k-viewer"}, json={
        "kind": "send", "content": [{"type": "file", "file_id": tiff.json()["file_id"]}]})
    assert r.status_code in (403, 404)
    monkeypatch.undo()
    assert client.get("/api/chat/files", params={"conversation_id": cid}).json()["items"] == []


def test_a_refused_quota_leaves_no_bytes(client, hub, monkeypatch):
    from hubzoid import attachments
    from hubzoid.chat.file_service import Err

    svc = client.app.state.chat.files
    store_root = svc._store()[1].root
    before = sorted(p for p in store_root.rglob("*") if p.is_file())
    real = svc.settings

    def tight():
        s = real()
        return {**s, "limits": {**s["limits"], "quota_bytes_per_person": 3}}

    monkeypatch.setattr(svc, "settings", tight)
    with pytest.raises(Err):
        attachments.save(hub, b"too much", "a.txt", "text/plain", "someone")
    assert sorted(p for p in store_root.rglob("*") if p.is_file()) == before


def test_a_fork_keeps_its_attachments(client):
    from tests.test_chat_operations import op, wait_run

    cid, b = new_conv(client)
    d = upload(client, b"a,b\n")
    sent = op(client, cid, {"kind": "send", "branch_id": b["id"], "expected_branch_revision": b["revision"],
                            "content": [{"type": "text", "text": "see"}, {"type": "file", "file_id": d["file_id"]}]})
    assert sent.status_code == 202, sent.text
    wait_run(client, sent.json()["run_id"])
    r = op(client, cid, {"kind": "fork", "source_head_id": sent.json()["assistant_message_id"], "scope": "path"})
    assert r.status_code == 201, r.text
    new = r.json()["new_conversation_id"]
    got = client.get(f"/api/conversations/{new}/files/{d['file_id']}")
    assert got.status_code == 200 and got.content == b"a,b\n"
    assert client.app.state.chat.files.local_for(new, d["file_id"]) == "orders.csv"


def test_linking_never_overwrites_a_taken_name(tmp_path):
    from hubzoid.chat.files import reserve_name

    src = tmp_path / "stored"
    src.write_bytes(b"new")
    folder = tmp_path / "chat"
    folder.mkdir()
    (folder / "a.csv").write_bytes(b"someone else's")
    name = reserve_name(folder, "a.csv", link_to=src)
    assert name == "a-2.csv" and (folder / "a.csv").read_bytes() == b"someone else's"
    assert (folder / name).read_bytes() == b"new" and (folder / name).stat().st_nlink == 2
