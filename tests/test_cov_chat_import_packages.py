"""Importing conversations (hubzoid.chat.portability): a package from outside
is untrusted. Each unsafe ZIP is refused (paths that escape, absolute or
backslash paths, symlinks, duplicates, unexpected files, ZIP bombs), each
integrity problem is recorded (hashes, sizes, corrupt lines), the format is
recognised (Hubzoid, Open WebUI, LibreChat) and the preview proposes what to
import, quarantine or reject."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from hubzoid.chat import portability as port
from hubzoid.chat.graph import GraphError


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def package(records=(), blobs=None, *, extra=None, manifest=None, records_sha=None, links=()):
    body = "\n".join(json.dumps(r) if not isinstance(r, str) else r for r in records).encode()
    blobs = blobs or {}
    man = {"format": port.FORMAT, "version": 1, "records_sha256": records_sha or sha(body),
           "blobs": [{"sha256": sha(b), "size": len(b)} for b in blobs.values()]}
    man.update(manifest or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(man))
        zf.writestr("records.jsonl", body)
        for b in blobs.values():
            zf.writestr(f"blobs/{sha(b)}", b)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0xA1ED << 16       # a symbolic link
            zf.writestr(info, "target")
    return buf.getvalue()


def refused(data, message):
    with pytest.raises(GraphError) as err:
        port.parse(data)
    assert message in err.value.message


@pytest.mark.parametrize("extra,message", [
    ({"../evil.txt": "x"}, "Unsafe path"),
    ({"/etc/passwd": "x"}, "Unsafe path"),
    ({"blobs\\x": "x"}, "Unsafe path"),
    ({"notes.txt": "x"}, "Unexpected file"),
])
def test_unsafe_or_unexpected_entries_are_refused(extra, message):
    refused(package(extra=extra), message)


def test_symlinks_duplicates_and_bombs_are_refused(monkeypatch):
    refused(package(links=["blobs/" + "a" * 64]), "Unsafe path")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf, pytest.warns(UserWarning, match="Duplicate name"):
        zf.writestr("manifest.json", "{}")
        zf.writestr("manifest.json", "{}")
    refused(buf.getvalue(), "Duplicate path")
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("records.jsonl", b"\n" * 5_000_000)          # compresses about a thousandfold
    refused(bomb.getvalue(), "expands beyond the allowed size")
    refused(b"PK\x03\x04 not really a zip", "not a readable ZIP package")


def test_manifest_and_records_must_match():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("records.jsonl", "")
    refused(buf.getvalue(), "no manifest.json or records.jsonl")
    refused(package(manifest={"format": "other"}), "not a hubzoid-exchange version 1 package")
    refused(package([{"type": "conversation"}], records_sha="0" * 64), "does not match its hash")


def test_integrity_problems_are_recorded_not_fatal():
    good, wrong = b"attachment bytes", b"other"
    data = package([{"type": "conversation", "source_id": "c1"}, "{corrupt"], {"good": good})
    fmt, records, blobs, problems = port.parse(data)
    assert fmt == port.FORMAT and records == [{"type": "conversation", "source_id": "c1"}]
    assert blobs == {sha(good): good}
    assert problems == [{"path": "records.jsonl:2", "code": "corrupt_record"}]

    lying = package([], {"x": good}, manifest={"blobs": [
        {"sha256": sha(good), "size": 3},                          # smaller than the entry
        {"sha256": sha(wrong), "size": len(wrong)},                # not in the package
        {"sha256": sha(good), "size": -1}]})
    _, _, blobs, problems = port.parse(lying)
    assert blobs == {} and {p["code"] for p in problems} == {"size_mismatch", "missing_blob", "too_large"}


def test_formats_and_their_refusals(monkeypatch):
    owui = [{"chat": {"title": "Leave", "history": {"messages": {}}}, "id": "c1", "title": "Leave"}]
    assert port.parse(json.dumps(owui).encode())[0] == "openwebui"
    libre = {"conversationId": "c1", "title": "Leave", "messages": []}
    assert port.parse(json.dumps(libre).encode())[0] == "librechat"
    refused(b"not json at all {", "not JSON, JSON lines or a hubzoid package")
    refused(json.dumps({"something": "else"}).encode(), "not recognised")
    monkeypatch.setattr(port, "MAX_UPLOAD", 10)
    with pytest.raises(GraphError) as err:
        port.parse(b"x" * 11)
    assert err.value.status == 413


def test_the_preview_proposes_a_disposition_for_each_record():
    records = [
        {"type": "conversation", "source_id": "c1"},
        {"type": "message", "source_id": "m1", "data": {"conversation": "c1", "role": "user"}},
        {"type": "message", "source_id": "m2", "data": {"conversation": "c9", "role": "user"}},
        {"type": "message", "source_id": "m3", "data": {"conversation": "c1", "parent": "m0", "role": "user"}},
        {"type": "message", "source_id": "m4", "data": {"conversation": "c1", "role": "tool"}},
        {"type": "message", "source_id": "m5", "data": {"conversation": "c1", "role": "user"},
         "refs": {"a": {"blob": "f" * 64}}},
        {"type": "prompt", "source_id": "p1"},
    ]
    got = {(o["type"], o["source_id"]): (o["disposition"], o["reason"])
           for o in port.preview(records, [{"path": "records.jsonl:9", "code": "corrupt_record"}], {})}
    assert got[("conversation", "c1")] == ("import", None)
    assert got[("message", "m1")] == ("import", None)
    assert got[("message", "m2")] == ("reject", "conversation_missing")
    assert got[("message", "m3")] == ("reject", "parent_missing")
    assert got[("message", "m4")] == ("quarantine", "unsupported")
    assert got[("message", "m5")] == ("reject", "missing_blob")
    assert got[("prompt", "p1")] == ("quarantine", "unsupported")
    assert got[("record", "records.jsonl:9")] == ("reject", "corrupt_record")
