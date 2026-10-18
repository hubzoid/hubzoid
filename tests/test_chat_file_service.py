"""Files with lasting identity, references and artifacts (HUB-74) through the API."""
from __future__ import annotations

import hashlib
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


def upload(client, data: bytes, name="orders.csv", mime="text/csv", conversation=None, headers=ORIGIN, chunk=None):
    up = client.post("/api/chat/files/uploads", headers=h(headers), json={
        "name": name, "mime_type": mime, "size_bytes": len(data), "conversation_id": conversation}).json()
    step = chunk or up["chunk_bytes"]
    off = 0
    while off < len(data):
        part = data[off:off + step]
        r = client.put(f"/api/chat/files/uploads/{up['upload_id']}/bytes", headers={
            **headers, "Upload-Offset": str(off), "Upload-Chunk-SHA256": hashlib.sha256(part).hexdigest()}, content=part)
        assert r.status_code == 200, r.text
        off = r.json()["offset"]
    done = client.post(f"/api/chat/files/uploads/{up['upload_id']}/complete", headers=headers,
                       json={"content_sha256": hashlib.sha256(data).hexdigest()})
    assert done.status_code == 202, done.text
    return ready(client, done.json()["file_id"], headers)


def ready(client, file_id, headers=ORIGIN):
    end = time.time() + 10
    while time.time() < end:
        d = client.get(f"/api/chat/files/{file_id}", headers=headers).json()
        if d["state"] != "validating":
            return d
        time.sleep(0.05)
    raise AssertionError("still validating")


def test_resumable_upload_offsets_replay_and_ranges(client):
    data = b"id,qty\n" + b"1,2\n" * 50
    up = client.post("/api/chat/files/uploads", headers=h(), json={"name": "orders.csv", "mime_type": "text/csv",
                                                                    "size_bytes": len(data)}).json()
    url = f"/api/chat/files/uploads/{up['upload_id']}/bytes"
    first = data[:100]
    assert client.put(url, headers={**ORIGIN, "Upload-Offset": "0"}, content=first).json()["offset"] == 100
    again = client.put(url, headers={**ORIGIN, "Upload-Offset": "0"}, content=first)  # a retry of a lost reply
    assert again.status_code == 200 and again.json()["offset"] == 100
    gap = client.put(url, headers={**ORIGIN, "Upload-Offset": "150"}, content=data[150:])
    assert gap.status_code == 409 and gap.json()["detail"]["offset"] == 100
    bad = client.put(url, headers={**ORIGIN, "Upload-Offset": "100", "Upload-Chunk-SHA256": "0" * 64}, content=data[100:])
    assert bad.status_code == 409 and bad.json()["detail"]["code"] == "checksum_mismatch"
    early = client.post(f"/api/chat/files/uploads/{up['upload_id']}/complete", headers=ORIGIN, json={})
    assert early.status_code == 409
    client.put(url, headers={**ORIGIN, "Upload-Offset": "100"}, content=data[100:])
    wrong = client.post(f"/api/chat/files/uploads/{up['upload_id']}/complete", headers=ORIGIN,
                        json={"content_sha256": "1" * 64})
    assert wrong.status_code == 409
    done = client.post(f"/api/chat/files/uploads/{up['upload_id']}/complete", headers=ORIGIN,
                       json={"content_sha256": hashlib.sha256(data).hexdigest()}).json()
    assert done["state"] in ("validating", "ready")
    d = ready(client, done["file_id"])
    assert d["state"] == "ready" and d["content_sha256"] == hashlib.sha256(data).hexdigest()
    part = client.get(f"/api/chat/files/{d['file_id']}/content", headers={"Range": "bytes=0-5"})
    assert part.status_code == 206 and part.content == data[:6] and part.headers["content-range"].endswith(f"/{len(data)}")
    full = client.get(f"/api/chat/files/{d['file_id']}/content")
    assert full.content == data and full.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in full.headers["content-disposition"]
    assert "qty" in client.get(f"/api/chat/files/{d['file_id']}/preview").json()["text"]


def test_types_limits_and_quarantine(client):
    r = client.post("/api/chat/files/uploads", headers=h(), json={
        "name": "macro.xlsm", "mime_type": "application/vnd.ms-excel.sheet.macroEnabled.12", "size_bytes": 10})
    assert r.status_code == 415
    r = client.post("/api/chat/files/uploads", headers=h(), json={"name": "big.csv", "mime_type": "text/csv",
                                                                   "size_bytes": 10**9})
    assert r.status_code == 413
    fake = upload(client, b"<html><script>alert(1)</script></html>", name="pic.png", mime="image/png")
    assert fake["state"] == "quarantined" and fake["processing_error"]


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


def test_legacy_upload_gets_a_lasting_id(client):
    cid, _ = new_conv(client)
    r = client.post(f"/api/conversations/{cid}/files", headers=ORIGIN,
                    files={"file": ("notes.txt", b"hello", "text/plain")}).json()
    assert r["file_id"] == "notes.txt" and r["id"]
    assert client.get(f"/api/chat/files/{r['id']}/content").content == b"hello"
    assert [f["file_id"] for f in client.get("/api/chat/files", params={"conversation_id": cid}).json()["items"]] == [r["id"]]


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
    assert "frame-src blob:" in csp and "default-src 'none'" in csp and "connect-src 'none'" in r.text and 'sandbox="allow-scripts"' in r.text


def test_storage_settings_and_s3_adapter(monkeypatch, tmp_path):
    from pydantic import ValidationError

    from hubzoid.chat import storage as st
    from hubzoid.chat.file_service import FilesSettings

    with pytest.raises(ValidationError):
        FilesSettings.model_validate({"storage": {"adapter": "s3"}})
    with pytest.raises(ValidationError):
        FilesSettings.model_validate({"storage": {"adapter": "s3", "bucket": "b", "credential_ref": "AKIA123:secret"}})
    with pytest.raises(ValidationError):
        FilesSettings.model_validate({"storage": {"adapter": "local", "bucket": "b"}})
    FilesSettings.model_validate({"storage": {"adapter": "s3", "bucket": "b", "endpoint": "http://127.0.0.1:9000",
                                              "credential_ref": "env:HZ_S3"}})
    objects: dict = {}

    class Body:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

    class Fake:
        def put_object(self, Bucket, Key, Body, ChecksumSHA256):
            import base64

            assert base64.b64decode(ChecksumSHA256) == hashlib.sha256(Body).digest()
            objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key, Range=None):
            data = objects[(Bucket, Key)]
            if Range:
                a, _, b = Range[6:].partition("-")
                data = data[int(a):int(b) + 1 if b else None]
            return {"Body": Body(data)}

        def head_object(self, Bucket, Key):
            from botocore.exceptions import ClientError

            if (Bucket, Key) not in objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(objects[(Bucket, Key)])}

        def delete_object(self, Bucket, Key):
            objects.pop((Bucket, Key), None)

    import boto3

    monkeypatch.setenv("HZ_S3", "AKIA:secret")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: Fake())
    s3 = st.S3Storage(bucket="b", endpoint=None, region="us-east-1", prefix="hz", credential_ref="env:HZ_S3")
    s3.put("ab/key", b"hello world")
    assert ("b", "hz/ab/key") in objects and s3.read("ab/key", 6, 10) == b"world" and s3.size("ab/key") == 11
    s3.delete("ab/key")
    assert not s3.exists("ab/key")
    local = st.LocalStorage(tmp_path)
    local.put("x/y", b"z")
    assert local.read("x/y") == b"z"
    with pytest.raises(st.StorageError):
        local.put("../escape", b"no")
