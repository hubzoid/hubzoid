"""Moving conversations in and out: the ``hubzoid-exchange`` package.

**Export** writes a ZIP with exactly ``manifest.json``, ``records.jsonl`` and
content-addressed ``blobs/<sha256>``: every readable branch of the chosen
conversations (all nodes, parent edges, the selected head), the person's own
organisation of them, and the attachments they hold. Nothing carrying
authority travels: no grants, runs, queues, pending requests, runtime
sessions, credentials or share tokens.

**Import** is previewed first. The preview reads a Hubzoid package, an Open
WebUI export (JSON array or one chat per line) or a LibreChat export
(``messages`` or ``messagesTree``), and returns every object with a
disposition (``import``, ``skip``, ``quarantine`` or ``reject``) and a digest
binding the bytes and choices. Apply takes that digest and creates everything
in one transaction with new ids, owned by the importer. A rejected batch
leaves nothing visible; a corrupt record, missing parent, zip traversal or
oversized input is rejected with its path. Imported text is history, never
instructions; nothing imported can run.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
import zipfile
from pathlib import Path

import sqlalchemy as sa

from ..control import canonical
from . import graph as g
from .store import _dumps, _message, conversations, messages, new_id

Err = g.GraphError
FORMAT = "hubzoid-exchange"
MAX_UPLOAD = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_BLOBS = 10_000
MAX_BLOB = 64 * 1024 * 1024
MAX_RECORD = 4 * 1024 * 1024
MAX_RATIO = 100
PREVIEW_SECONDS = 24 * 3600


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- export --------------------------------------------------------------------------------
def export_package(engine, hub_dir: Path, *, actor_id: str, conversation_ids: list[str], readable,
                   organization: dict | None = None) -> bytes:
    """A deterministic package of the conversations this person can read."""
    from .. import memory as memlib
    from .store import chat_key

    records: list[dict] = []
    blobs: dict[str, bytes] = {}
    with engine.connect() as conn:
        for cid in conversation_ids:
            conv_row, _ = readable(cid)
            view = g.get_view(conn, cid, actor_id) or {}
            msgs = [_message(r) for r in conn.execute(sa.select(messages).where(
                messages.c.conversation_id == cid, messages.c.deleted_at.is_(None))
                .order_by(messages.c.created_at, messages.c.id))]
            records.append({"version": 1, "type": "conversation", "source_id": cid,
                            "data": {"title": conv_row.get("title"), "agent": conv_row.get("agent"),
                                     "created_at": conv_row.get("created_at"), "head_id": view.get("view_head_id")},
                            "refs": {}})
            upload_dir = memlib.chat_upload_dir(Path(hub_dir), chat_key(conv_row))
            for m in msgs:
                refs: dict = {}
                content = []
                for i, p in enumerate(m["content"]):
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") in ("file", "image") and p.get("file_id"):
                        f = upload_dir / str(p["file_id"])
                        if f.is_file() and not f.is_symlink() and f.stat().st_size <= MAX_BLOB:
                            data = f.read_bytes()
                            h = _sha(data)
                            blobs[h] = data
                            refs[f"/content/{len(content)}"] = {"blob": h}
                        content.append({k: p.get(k) for k in ("type", "file_id", "name", "mime", "size")})
                    elif p.get("type") in ("text", "tool-call", "reasoning"):
                        content.append({k: v for k, v in p.items() if k in ("type", "text", "toolCallId",
                                                                             "toolName", "result")})
                    else:
                        content.append({"type": "unknown", "original_type": str(p.get("type"))})
                records.append({"version": 1, "type": "message", "source_id": m["id"],
                                "data": {"conversation": cid, "parent": m.get("parent_id"), "role": m["role"],
                                         "content": content, "status": m["status"] if m["status"] != "running"
                                         else "cancelled", "created_at": m.get("created_at"),
                                         "model": m.get("model")},
                                "refs": refs})
    if organization:
        records.append({"version": 1, "type": "organization", "source_id": "self", "data": organization, "refs": {}})
    body = "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records).encode()
    manifest = {"version": 1, "format": FORMAT, "created_at": time.time(), "records_sha256": _sha(body),
                "record_count": len(records),
                "blobs": [{"sha256": h, "size": len(b)} for h, b in sorted(blobs.items())]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        zf.writestr("records.jsonl", body)
        for h, data in sorted(blobs.items()):
            zf.writestr(f"blobs/{h}", data)
    return buf.getvalue()


# -- reading packages ------------------------------------------------------------------------
def _read_zip(data: bytes) -> tuple[list[dict], dict[str, bytes], list[dict]]:
    problems: list[dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise Err(422, "validation_failed", "That is not a readable ZIP package.")
    names = set()
    total = 0
    for info in zf.infolist():
        name = info.filename
        if name in names:
            raise Err(422, "validation_failed", f"Duplicate path {name!r} in the package.", path=name)
        names.add(name)
        if name.startswith("/") or ".." in name.split("/") or "\\" in name or (info.external_attr >> 28) == 0xA:
            raise Err(422, "validation_failed", f"Unsafe path {name!r} in the package.", path=name)
        total += info.file_size
        if total > MAX_EXPANDED or (info.compress_size and info.file_size / info.compress_size > MAX_RATIO):
            raise Err(422, "validation_failed", "The package expands beyond the allowed size.", path=name)
        if not (name in ("manifest.json", "records.jsonl") or (name.startswith("blobs/") and len(name) == 70)):
            raise Err(422, "validation_failed", f"Unexpected file {name!r} in the package.", path=name)
    budget = [MAX_EXPANDED]

    def read(name: str, limit: int) -> bytes:
        """Decompress at most ``limit`` bytes (and the package's remaining
        budget), whatever the entry's header claims."""
        cap = min(limit, budget[0])
        with zf.open(name) as fh:
            out = fh.read(cap + 1)
        if len(out) > cap:
            raise Err(422, "validation_failed", "The package expands beyond the allowed size.", path=name)
        budget[0] -= len(out)
        return out

    if "manifest.json" not in names or "records.jsonl" not in names:
        raise Err(422, "validation_failed", "The package has no manifest.json or records.jsonl.")
    try:
        manifest = json.loads(read("manifest.json", 16 * 1024 * 1024))
    except ValueError:
        raise Err(422, "validation_failed", "manifest.json is not valid JSON.", path="manifest.json")
    if manifest.get("format") != FORMAT or manifest.get("version") != 1:
        raise Err(422, "validation_failed", "This is not a hubzoid-exchange version 1 package.")
    body = read("records.jsonl", MAX_EXPANDED)
    if _sha(body) != manifest.get("records_sha256"):
        raise Err(422, "validation_failed", "records.jsonl does not match its hash.", path="records.jsonl")
    records = []
    for n, line in enumerate(body.decode("utf-8").splitlines(), 1):
        if len(line) > MAX_RECORD:
            problems.append({"path": f"records.jsonl:{n}", "code": "too_large"})
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            problems.append({"path": f"records.jsonl:{n}", "code": "corrupt_record"})
    blobs = {}
    declared = manifest.get("blobs") or []
    if not isinstance(declared, list) or len(declared) > MAX_BLOBS:
        raise Err(422, "validation_failed", "The package has too many records or files.")
    for b in declared:
        name = f"blobs/{b.get('sha256')}"
        if name not in names:
            problems.append({"path": name, "code": "missing_blob"})
            continue
        size = b.get("size")
        if not isinstance(size, int) or not 0 <= size <= MAX_BLOB:
            problems.append({"path": name, "code": "too_large"})
            continue
        try:
            data = read(name, size)
        except Err:
            problems.append({"path": name, "code": "size_mismatch"})
            continue
        if _sha(data) != b["sha256"]:
            problems.append({"path": name, "code": "hash_mismatch"})
            continue
        blobs[b["sha256"]] = data
    if len(records) > MAX_RECORDS or len(blobs) > MAX_BLOBS:
        raise Err(422, "validation_failed", "The package has too many records or files.")
    return records, blobs, problems


def _from_openwebui(chats: list[dict]) -> list[dict]:
    out = []
    for chat in chats:
        cid = str(chat.get("id") or uuid.uuid4())
        body = chat.get("chat") or {}
        history = (body.get("history") or {}).get("messages") or {}
        out.append({"version": 1, "type": "conversation", "source_id": cid,
                    "data": {"title": chat.get("title") or body.get("title"), "agent": None,
                             "created_at": chat.get("created_at"),
                             "head_id": (body.get("history") or {}).get("currentId")}, "refs": {}})
        for mid, m in history.items():
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            out.append({"version": 1, "type": "message", "source_id": str(mid),
                        "data": {"conversation": cid, "parent": m.get("parentId"), "role": role,
                                 "content": [{"type": "text", "text": str(m.get("content") or "")}],
                                 "status": "complete", "created_at": m.get("timestamp"), "model": m.get("model")},
                        "refs": {}})
    return out


def _from_librechat(doc: dict) -> list[dict]:
    cid = str(doc.get("conversationId") or uuid.uuid4())
    out = [{"version": 1, "type": "conversation", "source_id": cid,
            "data": {"title": doc.get("title"), "agent": None, "created_at": None, "head_id": None}, "refs": {}}]
    flat: list[dict] = []
    if isinstance(doc.get("messages"), list):
        flat = doc["messages"]
    elif isinstance(doc.get("messagesTree"), list):
        stack = list(doc["messagesTree"])
        while stack:
            node = stack.pop()
            flat.append(node)
            stack.extend(node.get("children") or [])
    for m in flat:
        parent = m.get("parentMessageId")
        if parent in ("00000000-0000-0000-0000-000000000000", None):
            parent = None
        out.append({"version": 1, "type": "message", "source_id": str(m.get("messageId")),
                    "data": {"conversation": cid, "parent": parent,
                             "role": "user" if m.get("isCreatedByUser") else "assistant",
                             "content": [{"type": "text", "text": str(m.get("text") or "")}], "status": "complete",
                             "created_at": None, "model": m.get("model")}, "refs": {}})
    return out


def parse(data: bytes) -> tuple[str, list[dict], dict[str, bytes], list[dict]]:
    if len(data) > MAX_UPLOAD:
        raise Err(413, "validation_failed", "Imports are at most 256 MiB.")
    if data[:2] == b"PK":
        records, blobs, problems = _read_zip(data)
        return FORMAT, records, blobs, problems
    text = data.decode("utf-8", errors="strict")
    try:
        doc = json.loads(text)
    except ValueError:
        try:
            doc = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            raise Err(422, "validation_failed", "That file is not JSON, JSON lines or a hubzoid package.")
    if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "chat" in doc[0]:
        return "openwebui", _from_openwebui(doc), {}, []
    if isinstance(doc, dict) and "conversationId" in doc and ("messages" in doc or "messagesTree" in doc):
        return "librechat", _from_librechat(doc), {}, []
    raise Err(422, "validation_failed", "That format is not recognised (Hubzoid, Open WebUI or LibreChat).")


def preview(records: list[dict], problems: list[dict], blobs: dict[str, bytes]) -> list[dict]:
    """One entry per object with its proposed disposition and why."""
    ids = {(r.get("type"), r.get("source_id")) for r in records}
    objects = []
    for r in records:
        t, sid, data = r.get("type"), r.get("source_id"), r.get("data") or {}
        entry = {"type": t, "source_id": sid, "disposition": "import", "reason": None}
        if t == "message":
            if ("conversation", data.get("conversation")) not in ids:
                entry.update(disposition="reject", reason="conversation_missing")
            elif data.get("parent") and ("message", data.get("parent")) not in ids:
                entry.update(disposition="reject", reason="parent_missing")
            elif data.get("role") not in ("user", "assistant"):
                entry.update(disposition="quarantine", reason="unsupported")
            for ref in (r.get("refs") or {}).values():
                if ref.get("blob") and ref["blob"] not in blobs:
                    entry.update(disposition="reject", reason="missing_blob")
        elif t not in ("conversation", "organization"):
            entry.update(disposition="quarantine", reason="unsupported")
        objects.append(entry)
    for p in problems:
        objects.append({"type": "record", "source_id": p["path"], "disposition": "reject", "reason": p["code"]})
    return objects


def apply(engine, hub_dir: Path, hub: str, *, actor_id: str, actor_email: str, agent: str, records: list[dict],
          blobs: dict[str, bytes], dispositions: dict[str, str], operation_id: str, key: str | None = None,
          request: dict | None = None) -> dict:
    """Create the approved objects. Attachments are written and checked first;
    then the conversations and messages commit in one transaction together with
    the import's receipt (keyed by ``key``), so a retry after a crash returns
    the first result and never imports twice. A failure removes staged files."""
    import shutil

    from .. import memory as memlib
    from ..control import deploy, idempotency
    from .store import chat_key

    convs = {r["source_id"]: r for r in records if r.get("type") == "conversation"
             and dispositions.get(f"conversation:{r['source_id']}", "import") == "import"}
    msgs = [r for r in records if r.get("type") == "message"
            and dispositions.get(f"message:{r['source_id']}", "import") == "import"
            and r["data"].get("conversation") in convs]
    conv_map = {sid: new_id("c_") for sid in convs}
    staged: list[Path] = []

    def unstage() -> None:
        for d in staged:
            shutil.rmtree(d, ignore_errors=True)

    files = 0
    try:
        for r in msgs:
            for i, p in enumerate(r["data"].get("content") or []):
                ref = (r.get("refs") or {}).get(f"/content/{i}")
                if not (ref and ref.get("blob") in blobs and p.get("type") in ("file", "image")):
                    continue
                name = Path(str(p.get("file_id"))).name
                if not name or name.startswith("."):
                    continue
                target = memlib.chat_upload_dir(Path(hub_dir), chat_key({"id": conv_map[r["data"]["conversation"]],
                                                                         "source": "web"}))
                if target not in staged:
                    target.mkdir(parents=True, exist_ok=True)
                    staged.append(target)
                (target / name).write_bytes(blobs[ref["blob"]])
                if _sha((target / name).read_bytes()) != ref["blob"]:
                    raise OSError(f"{name} was not stored intact")
                files += 1
    except OSError as exc:
        unstage()
        raise Err(507, "storage_failed", f"The files of this import could not be stored ({type(exc).__name__}). "
                                         "Nothing was imported.")

    def change(conn):
        return 201, _apply_graph(conn, hub, actor_id=actor_id, actor_email=actor_email, agent=agent, convs=convs,
                                 msgs=msgs, conv_map=conv_map, operation_id=operation_id, files=files)

    try:
        out = idempotency.run(engine, deployment_id=deploy.deployment_id(Path(hub_dir)), actor_id=actor_id,
                              route="POST /api/chat/imports", key=key, body=request or {}, change=change,
                              hub_id=hub, kind="chat.import", operation_id=operation_id)
    except BaseException:
        unstage()
        raise
    if out.replayed:
        unstage()  # the first attempt's conversations and files are the import
    return out.body


def _apply_graph(conn, hub: str, *, actor_id: str, actor_email: str, agent: str, convs: dict, msgs: list,
                 conv_map: dict, operation_id: str, files: int) -> dict:
    mapping: dict[str, str] = {}
    t = time.time()
    for sid, r in convs.items():
        cid = conv_map[sid]
        conn.execute(conversations.insert().values(
            id=cid, owner_id=actor_id, owner_email=actor_email, hub=hub, agent=agent,
            title=(r["data"].get("title") or "Imported conversation")[:200], title_source="user", archived=0,
            head_id=None, source="web", created_at=t, updated_at=t, graph_revision=0, temporary=0,
            expires_at=None, deleted_at=None))
    # parents before children
    by_id = {r["source_id"]: r for r in msgs}
    ordered, seen = [], set()

    for sid in list(by_id):
        chain, on_chain, node = [], set(), sid
        while node and node in by_id and node not in seen and node not in on_chain:
            chain.append(node)
            on_chain.add(node)
            node = by_id[node]["data"].get("parent")
        for n in reversed(chain):
            seen.add(n)
            ordered.append(by_id[n])
    for r in ordered:
        d = r["data"]
        nid = new_id("m_")
        mapping[r["source_id"]] = nid
        content = list(d.get("content") or [])
        text = "\n\n".join(str(p.get("text") or "") for p in content if p.get("type") == "text")
        conn.execute(messages.insert().values(
            id=nid, conversation_id=conv_map[d["conversation"]], parent_id=mapping.get(d.get("parent")),
            role=d["role"], content=_dumps(content), text=text,
            status=d.get("status") if d.get("status") in ("complete", "cancelled", "error") else "complete",
            error=None, model=d.get("model"), usage=None, created_at=float(d.get("created_at") or t) if
            isinstance(d.get("created_at"), (int, float)) else t, updated_at=t, branch_id=None, revises_id=None,
            author_id=actor_id, editor_id=None, run_id=None, requested_model=None, historical_only=1,
            context_changed=0, deleted_at=None))
    for sid, cid in conv_map.items():
        g.ensure_initial_branch(conn, {"id": cid, "owner_id": actor_id}, actor_id)
        head = mapping.get(convs[sid]["data"].get("head_id") or "")
        g.set_view(conn, cid, actor_id, None, head)
    return {"operation_id": operation_id, "conversations": conv_map, "messages": len(mapping), "files": files}


class ImportStore:
    """Private, expiring import previews (bytes and parsed records) on disk."""

    def __init__(self, hub_dir: Path):
        from ..control import deploy

        self.root = deploy.state_root(Path(hub_dir)) / ".hubzoid" / "imports"

    def save(self, actor_id: str, data: bytes, fmt: str, objects: list[dict]) -> dict:
        pid = str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{pid}.bin").write_bytes(data)
        digest = canonical.digest({"bytes": _sha(data), "objects": objects})
        meta = {"id": pid, "actor_id": actor_id, "format": fmt, "digest": digest, "objects": objects, "revision": 1,
                "expires_at": time.time() + PREVIEW_SECONDS}
        (self.root / f"{pid}.json").write_text(json.dumps(meta))
        return meta

    def load(self, pid: str, actor_id: str) -> tuple[dict, bytes]:
        if not pid.replace("-", "").isalnum():
            raise Err(404, "not_found", "That import preview was not found.")
        meta_path = self.root / f"{pid}.json"
        if not meta_path.is_file():
            raise Err(404, "not_found", "That import preview was not found.")
        meta = json.loads(meta_path.read_text())
        if meta["actor_id"] != actor_id:
            raise Err(404, "not_found", "That import preview was not found.")
        if meta["expires_at"] < time.time() and "result" not in meta:
            raise Err(409, "preview_expired", "The import preview expired. Upload the file again.")
        if "result" in meta:  # already applied: a retry returns the recorded result
            return meta, b""
        return meta, (self.root / f"{pid}.bin").read_bytes()

    def done(self, pid: str, result: dict) -> None:
        meta_path = self.root / f"{pid}.json"
        meta = json.loads(meta_path.read_text())
        meta["result"] = result
        meta_path.write_text(json.dumps(meta))
        (self.root / f"{pid}.bin").unlink(missing_ok=True)


def digest_with(meta: dict, dispositions: dict[str, str]) -> str:
    return canonical.digest({"preview": meta["digest"], "dispositions": dict(sorted(dispositions.items()))})

