"""Files and artifacts with lasting identity (HUB-74).

A **file** is immutable bytes with an opaque id, its SHA-256, size, type and
original name, stored through an adapter (``storage``: local disk or one
S3-compatible bucket). Who may read it follows its **references**: a file is
readable by its owner and by anyone who may read a conversation that refers to
it, checked at every read (never a cached grant). Removing a reference never
deletes bytes another reference still uses; deleting a file needs zero active
references, tombstones it first (reads stop at once) and then removes the bytes.
There is no default quota and no automatic deletion.

Uploads are resumable: create, send bounded chunks at the server's offset
(a replayed identical chunk returns the same offset), complete with the
whole-file hash. A completed file is ``validating`` until its type is checked,
then ``ready`` (or ``quarantined``); only ready files can be attached.

The conversation keeps a working copy of each attached file in its own uploads
folder (``memory.chat_upload_dir``), which is what the agent's tools read, so
every runtime sees attachments the same way.

An **artifact** is a document the agent produced in a conversation (its chat
artifacts folder) or that a person revised. Each version is a file, numbered,
with its author, run and provenance (generated or edited); an edit names the
version it started from and the artifact's revision.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..control import canonical
from . import graph as g
from . import storage as st
from .store import conversations

log = logging.getLogger("hubzoid.chat.files")
metadata = sa.MetaData()
Err = g.GraphError

CHUNK_BYTES = 4 * 1024 * 1024
UPLOAD_SECONDS = 24 * 3600
DEFAULT_TYPES = [
    "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", "text/plain", "text/markdown",
    "text/csv", "application/json", "image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml",
    "text/html", "audio/mpeg", "audio/wav", "video/mp4"]
MACRO_TYPES = {"application/vnd.ms-word.document.macroenabled.12", "application/vnd.ms-excel.sheet.macroenabled.12",
               "application/vnd.ms-powerpoint.presentation.macroenabled.12"}
_REF = re.compile(r"^(env:[A-Z_][A-Z0-9_]{0,63}|secret:(deployment|hub/[^/]+|channel/[^/]+/[^/]+)/[\w.-]+)$")


# -- settings ------------------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageSettings(_Strict):
    adapter: Literal["local", "s3"] = "local"
    local_root: str = ".hubzoid/files"
    bucket: str | None = None
    endpoint: str | None = None
    region: str | None = None
    prefix: str = "hubzoid"
    credential_ref: str | None = None
    s3_addressing_style: Literal["auto", "virtual", "path"] = "auto"

    @field_validator("local_root", "prefix")
    @classmethod
    def _relative(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/") or "\\" in v:
            raise ValueError("a relative path without '..'")
        return v

    @field_validator("credential_ref")
    @classmethod
    def _ref(cls, v):
        if v is not None and not _REF.match(v):
            raise ValueError("env:NAME or secret:<scope>/name, never a credential value")
        return v

    @model_validator(mode="after")
    def _adapter(self):
        if self.adapter == "s3":
            if not self.bucket:
                raise ValueError("storage.bucket is required for s3")
            if self.endpoint and not (self.endpoint.startswith("https://")
                                      or re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/?", self.endpoint)):
                raise ValueError("storage.endpoint must be https:// (http only for a loopback test endpoint)")
        elif any((self.bucket, self.endpoint, self.region, self.credential_ref)):
            raise ValueError("bucket, endpoint, region and credential_ref are for s3 only; leave them null")
        return self


class LimitSettings(_Strict):
    max_file_bytes: int = Field(26_214_400, ge=1, le=2_147_483_648)
    max_files_per_message: int = Field(10, ge=1, le=100)
    allowed_mime_types: list[str] = Field(default_factory=lambda: list(DEFAULT_TYPES))
    quota_bytes_per_person: int | None = Field(None, ge=1)

    @field_validator("allowed_mime_types")
    @classmethod
    def _no_macros(cls, v):
        bad = [m for m in v if m.lower() in MACRO_TYPES]
        if bad:
            raise ValueError(f"macro-enabled Office types are not allowed: {', '.join(bad)}")
        return [m.lower() for m in v]


class ProcessingSettings(_Strict):
    max_seconds: int = Field(60, ge=1, le=3600)
    max_expanded_bytes: int = Field(104_857_600, ge=1)


class RenderSettings(_Strict):
    markdown_user: bool = False
    markdown_assistant: bool = True
    auto_open_artifact: bool = True
    code_collapse_lines: int = Field(40, ge=5, le=10_000)
    max_preview_bytes: int = Field(5_242_880, ge=1024, le=104_857_600)
    timeout_ms: int = Field(10_000, ge=500, le=120_000)
    external_media: Literal["ask", "deny"] = "ask"


class FilesSettings(_Strict):
    enabled: bool = True
    storage: StorageSettings = Field(default_factory=StorageSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)


def register() -> None:
    from .config import namespaces, register_namespace

    ns = namespaces().get("files")
    if ns is None or not ns.installed:
        register_namespace("files", FilesSettings, owner="HUB-74")


register()

# -- tables -------------------------------------------------------------------------------
storage_configs = sa.Table(
    "hz_chat_storage_configs", metadata,
    sa.Column("id", sa.String(80), primary_key=True),
    sa.Column("adapter", sa.String(16), nullable=False),
    sa.Column("config_json", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
)

files = sa.Table(
    "hz_chat_files", metadata,
    sa.Column("file_id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("owner_principal_id", sa.String(64), nullable=False, index=True),
    sa.Column("storage_config_id", sa.String(80), nullable=False),
    sa.Column("object_key", sa.String(255), nullable=False),
    sa.Column("content_sha256", sa.String(64), nullable=False),
    sa.Column("size_bytes", sa.Integer, nullable=False),
    sa.Column("mime_type", sa.String(255), nullable=False),
    sa.Column("original_name", sa.String(255), nullable=False),
    sa.Column("origin", sa.String(16), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("processing_error", sa.Text),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)

uploads = sa.Table(
    "hz_chat_file_uploads", metadata,
    sa.Column("upload_id", sa.String(64), primary_key=True),
    sa.Column("file_id", sa.String(64), nullable=False),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("owner_principal_id", sa.String(64), nullable=False),
    sa.Column("conversation_id", sa.String(64)),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("mime_type", sa.String(255), nullable=False),
    sa.Column("size_bytes", sa.Integer, nullable=False),
    sa.Column("expected_sha256", sa.String(64)),
    sa.Column("next_offset", sa.Integer, nullable=False),
    sa.Column("last_chunk_sha256", sa.String(64)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("idempotency_key", sa.String(255)),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.UniqueConstraint("owner_principal_id", "hub_id", "idempotency_key", name="uq_hz_file_upload_key"),
)

references = sa.Table(
    "hz_chat_file_references", metadata,
    sa.Column("reference_id", sa.String(64), primary_key=True),
    sa.Column("file_id", sa.String(64), nullable=False, index=True),
    sa.Column("resource_type", sa.String(16), nullable=False),
    sa.Column("resource_id", sa.String(64), nullable=False),
    sa.Column("conversation_id", sa.String(64), index=True),
    sa.Column("local_name", sa.String(255)),
    sa.Column("created_by", sa.String(64), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)

artifacts = sa.Table(
    "hz_chat_artifacts", metadata,
    sa.Column("artifact_id", sa.String(64), primary_key=True),
    sa.Column("hub_id", sa.String(255), nullable=False),
    sa.Column("owner_principal_id", sa.String(64), nullable=False),
    sa.Column("conversation_id", sa.String(64), nullable=False, index=True),
    sa.Column("branch_id", sa.String(64)),
    sa.Column("title", sa.String(255), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("current_version_id", sa.String(64)),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.UniqueConstraint("conversation_id", "title", name="uq_hz_artifact_title"),
)

artifact_versions = sa.Table(
    "hz_chat_artifact_versions", metadata,
    sa.Column("version_id", sa.String(64), primary_key=True),
    sa.Column("artifact_id", sa.String(64), nullable=False, index=True),
    sa.Column("file_id", sa.String(64), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("base_version_id", sa.String(64)),
    sa.Column("author_id", sa.String(64), nullable=False),
    sa.Column("run_id", sa.String(64)),
    sa.Column("message_id", sa.String(64)),
    sa.Column("provenance", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.UniqueConstraint("artifact_id", "ordinal", name="uq_hz_artifact_ordinal"),
)

TABLES = (storage_configs, files, uploads, references, artifacts, artifact_versions)
LIVE = ("validating", "ready")


def _now() -> float:
    return time.time()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_kind(name: str, mime: str) -> str:
    low = name.lower()
    if low.endswith((".html", ".htm")) or mime == "text/html":
        return "html"
    if low.endswith(".svg") or mime == "image/svg+xml":
        return "svg"
    if low.endswith((".md", ".markdown")):
        return "markdown"
    if mime.startswith("image/"):
        return "image"
    if low.endswith(".pdf"):
        return "pdf"
    if mime.startswith("text/") or low.endswith((".csv", ".json", ".txt", ".py", ".js", ".ts", ".sql", ".yaml")):
        return "text"
    return "download"


class FileService:
    def __init__(self, hub_dir: Path, engine, hub: str, chat_engine=None):
        self.hub_dir = Path(hub_dir)
        self.engine = engine
        self.hub = hub
        self.chat = chat_engine  # ChatEngine: conversation roles for reference-based access
        self._adapters: dict[str, object] = {}
        #: called (from any thread) after an event commits: notify(conversation_id)
        self.notify = lambda conversation_id: None
        #: (conn, "<hub>:<collection id>", actor) -> bool, set by the sources service
        self.collection_readable = None

    # -- settings and storage --------------------------------------------------------------
    def settings(self) -> dict:
        from .config import service_for

        try:
            return service_for(self.hub_dir).active().document["files"]
        except Exception:  # noqa: BLE001 - the defaults are always safe
            return FilesSettings().model_dump()

    def _store(self, config_id: str | None = None):
        """The adapter of ``config_id`` (the one that wrote an object), or the
        current default, recording its redacted configuration once."""
        if config_id is None:
            cfg = self.settings()["storage"]
            config_id = "sc_" + canonical.digest(cfg)[7:31]
            if config_id not in self._adapters:
                with self.engine.begin() as conn:
                    if conn.execute(sa.select(storage_configs.c.id).where(storage_configs.c.id == config_id)).first() is None:
                        conn.execute(storage_configs.insert().values(id=config_id, adapter=cfg["adapter"],
                                                                     config_json=json.dumps(cfg), created_at=_now()))
                self._adapters[config_id] = st.for_settings(self.hub_dir, cfg)
            return config_id, self._adapters[config_id]
        if config_id not in self._adapters:
            with self.engine.connect() as conn:
                row = conn.execute(sa.select(storage_configs).where(storage_configs.c.id == config_id)).first()
            if row is None:
                raise Err(503, "storage_unavailable", "The storage that holds this file is no longer configured.")
            self._adapters[config_id] = st.for_settings(self.hub_dir, json.loads(row.config_json))
        return config_id, self._adapters[config_id]

    def _staging(self, upload_id: str) -> Path:
        from ..control import deploy

        p = deploy.state_root(self.hub_dir) / ".hubzoid" / "files-staging"
        p.mkdir(parents=True, exist_ok=True)
        return p / upload_id

    def _check_type(self, name: str, mime: str, size: int) -> str:
        from .. import uploads as uploads_lib

        mime = (mime or "").split(";")[0].strip().lower() or uploads_lib.guess_mime(name)
        if mime == "application/octet-stream":
            mime = uploads_lib.guess_mime(name, fallback=mime)
        lim = self.settings()["limits"]
        if size > lim["max_file_bytes"]:
            raise Err(413, "upload_too_large", f"That file is larger than the {lim['max_file_bytes'] // 1048576} MiB "
                                               "limit.", limit=lim["max_file_bytes"])
        if mime in MACRO_TYPES or mime not in lim["allowed_mime_types"]:
            raise Err(415, "type_not_allowed", f"Files of type {mime} can't be attached here.",
                      allowed=lim["allowed_mime_types"])
        return mime

    def _check_quota(self, conn, owner_id: str, size: int) -> None:
        quota = self.settings()["limits"]["quota_bytes_per_person"]
        if quota is None:
            return
        used = conn.execute(sa.select(sa.func.coalesce(sa.func.sum(files.c.size_bytes), 0)).where(
            files.c.owner_principal_id == owner_id, files.c.deleted_at.is_(None))).scalar() or 0
        pending = conn.execute(sa.select(sa.func.coalesce(sa.func.sum(uploads.c.size_bytes), 0)).where(
            uploads.c.owner_principal_id == owner_id, uploads.c.state == "uploading")).scalar() or 0
        if used + pending + size > quota:
            raise Err(429, "quota_exceeded", "This would go over your storage allowance. Delete files you no longer "
                                             "need.", remaining_bytes=max(0, quota - used - pending))

    # -- access ----------------------------------------------------------------------------------
    def _row(self, conn, file_id: str):
        return conn.execute(sa.select(files).where(files.c.file_id == file_id, files.c.hub_id == self.hub)).first()

    def _readable_conversation(self, conn, conversation_id: str, actor) -> bool:
        if self.chat is None:
            return False
        conv = conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()
        return conv is not None and self.chat.role(conn, dict(conv._mapping), actor) is not None

    def _may_read(self, conn, row, actor) -> bool:
        if row is None or row.deleted_at is not None:
            return False
        if row.owner_principal_id == actor.id:
            return True
        refs = conn.execute(sa.select(references.c.conversation_id, references.c.resource_type,
                                      references.c.resource_id).where(
            references.c.file_id == row.file_id, references.c.deleted_at.is_(None))).all()
        if any(r.conversation_id and self._readable_conversation(conn, r.conversation_id, actor) for r in refs):
            return True
        # A file in a collection of sources is readable by that collection's readers.
        return self.collection_readable is not None and any(
            r.resource_type == "collection" and self.collection_readable(conn, r.resource_id, actor) for r in refs)

    def _readable(self, conn, file_id: str, actor):
        row = self._row(conn, file_id)
        if not self._may_read(conn, row, actor):
            raise Err(404, "file_not_found", "That file was not found.")
        return row

    def describe(self, row, actor=None) -> dict:
        owner = actor is not None and row.owner_principal_id == actor.id
        return {"file_id": row.file_id, "name": row.original_name, "mime_type": row.mime_type,
                "size_bytes": row.size_bytes, "content_sha256": row.content_sha256, "state": row.state,
                "origin": row.origin, "revision": row.revision, "created_at": row.created_at,
                "processing_error": row.processing_error, "owned": owner,
                "actions": ["download", "preview", "reuse"] + (["delete"] if owner else [])}

    # -- creating files --------------------------------------------------------------------------
    def register_bytes(self, actor_id: str, name: str, mime: str, data: bytes, *, origin: str = "upload",
                       conversation_id: str | None = None, local_name: str | None = None,
                       check_type: bool = True) -> dict:
        """Store bytes as a new ready file (already received and bounded by the
        caller), with an optional conversation reference."""
        if check_type:
            mime = self._check_type(name, mime, len(data))
        config_id, store = self._store()
        key = st.new_key()
        store.put(key, data)
        fid = str(uuid.uuid4())
        t = _now()
        with self.engine.begin() as conn:
            self._check_quota(conn, actor_id, len(data))
            conn.execute(files.insert().values(
                file_id=fid, hub_id=self.hub, owner_principal_id=actor_id, storage_config_id=config_id, object_key=key,
                content_sha256=sha256(data), size_bytes=len(data), mime_type=mime, original_name=name[:255],
                origin=origin, state="ready", processing_error=None, revision=1, created_at=t, deleted_at=None))
            if conversation_id:
                self._add_ref(conn, fid, "conversation", conversation_id, conversation_id, actor_id, local_name)
            row = self._row(conn, fid)
        return self.describe(row)

    def start_upload(self, actor, body: dict, *, key: str | None) -> dict:
        if not self.settings()["enabled"]:
            raise Err(403, "feature_disabled", "File uploads are turned off for this deployment.")
        from .files import safe_upload_name

        name = safe_upload_name(str(body.get("name") or ""))
        size = body.get("size_bytes")
        if not isinstance(size, int) or size < 0:
            raise Err(422, "validation_failed", "size_bytes is the file's size in bytes.")
        mime = self._check_type(name, str(body.get("mime_type") or ""), size)
        expected = body.get("content_sha256")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise Err(422, "validation_failed", "content_sha256 is 64 lowercase hex characters.")
        cid = body.get("conversation_id")
        with self.engine.begin() as conn:
            if cid and not self._writable_conversation(conn, cid, actor):
                raise Err(404, "not_found", "That conversation was not found.")
            if key:
                old = conn.execute(sa.select(uploads).where(uploads.c.owner_principal_id == actor.id,
                                                            uploads.c.hub_id == self.hub,
                                                            uploads.c.idempotency_key == key)).first()
                if old is not None:
                    if (old.name, old.size_bytes, old.conversation_id) != (name, size, cid):
                        raise Err(409, "idempotency_key_reused", "That Idempotency-Key was used for another upload.")
                    return self._upload_json(old)
            self._check_quota(conn, actor.id, size)
            uid, fid = str(uuid.uuid4()), str(uuid.uuid4())
            conn.execute(uploads.insert().values(
                upload_id=uid, file_id=fid, hub_id=self.hub, owner_principal_id=actor.id, conversation_id=cid,
                name=name, mime_type=mime, size_bytes=size, expected_sha256=expected, next_offset=0,
                last_chunk_sha256=None, state="uploading", idempotency_key=key, expires_at=_now() + UPLOAD_SECONDS))
            row = conn.execute(sa.select(uploads).where(uploads.c.upload_id == uid)).first()
        self._staging(uid).write_bytes(b"")
        return self._upload_json(row)

    @staticmethod
    def _upload_json(r) -> dict:
        return {"upload_id": r.upload_id, "file_id": r.file_id, "offset": r.next_offset, "chunk_bytes": CHUNK_BYTES,
                "size_bytes": r.size_bytes, "state": r.state, "expires_at": r.expires_at}

    def _upload(self, conn, upload_id: str, actor):
        r = conn.execute(sa.select(uploads).where(uploads.c.upload_id == upload_id,
                                                  uploads.c.owner_principal_id == actor.id)).first()
        if r is None:
            raise Err(404, "not_found", "That upload was not found.")
        if r.state == "uploading" and r.expires_at < _now():
            raise Err(410, "upload_expired", "This upload expired. Start it again.")
        return r

    def put_chunk(self, actor, upload_id: str, offset: int, data: bytes, chunk_sha: str | None) -> dict:
        if len(data) > CHUNK_BYTES:
            raise Err(413, "chunk_too_large", f"Send at most {CHUNK_BYTES} bytes per chunk.")
        if chunk_sha and sha256(data) != chunk_sha:
            raise Err(409, "checksum_mismatch", "That chunk did not arrive intact. Send it again.")
        with self.engine.begin() as conn:
            r = self._upload(conn, upload_id, actor)
            if r.state != "uploading":
                raise Err(409, "invalid_state", "This upload is no longer accepting bytes.")
            digest = sha256(data)
            # The identical chunk again (a retry after a lost reply): same answer.
            if offset + len(data) == r.next_offset and digest == r.last_chunk_sha256:
                return {"offset": r.next_offset}
            if offset != r.next_offset:
                raise Err(409, "offset_conflict", "Resume from the server's offset.", offset=r.next_offset)
            if r.next_offset + len(data) > r.size_bytes:
                raise Err(409, "offset_conflict", "That is more than the declared size.", offset=r.next_offset)
            with open(self._staging(upload_id), "r+b") as fh:
                fh.seek(offset)
                fh.write(data)
                fh.truncate(offset + len(data))
            conn.execute(uploads.update().where(uploads.c.upload_id == upload_id).values(
                next_offset=offset + len(data), last_chunk_sha256=digest))
        return {"offset": offset + len(data)}

    def complete(self, actor, upload_id: str, content_sha: str | None) -> dict:
        with self.engine.begin() as conn:
            r = self._upload(conn, upload_id, actor)
            if r.state == "complete":
                return self.describe(self._row(conn, r.file_id), actor)
            if r.state != "uploading":
                raise Err(409, "invalid_state", "This upload was cancelled.")
            if r.next_offset != r.size_bytes:
                raise Err(409, "upload_incomplete", "Not every byte has arrived yet.", offset=r.next_offset)
        data = self._staging(upload_id).read_bytes()
        digest = sha256(data)
        want = content_sha or r.expected_sha256
        if want and want != digest:
            raise Err(409, "checksum_mismatch", "The file did not arrive intact. Start the upload again.")
        config_id, store = self._store()
        key = st.new_key()
        store.put(key, data)
        t = _now()
        with self.engine.begin() as conn:
            claimed = conn.execute(uploads.update().where(uploads.c.upload_id == upload_id,
                                                          uploads.c.state == "uploading")
                                   .values(state="complete")).rowcount
            if claimed != 1:  # a concurrent completion won
                store.delete(key)
                return self.describe(self._row(conn, r.file_id), actor)
            conn.execute(files.insert().values(
                file_id=r.file_id, hub_id=self.hub, owner_principal_id=actor.id, storage_config_id=config_id,
                object_key=key, content_sha256=digest, size_bytes=len(data), mime_type=r.mime_type,
                original_name=r.name, origin="upload", state="validating", processing_error=None, revision=1,
                created_at=t, deleted_at=None))
            if r.conversation_id:
                self._add_ref(conn, r.file_id, "conversation", r.conversation_id, r.conversation_id, actor.id, None)
            row = self._row(conn, r.file_id)
        self._staging(upload_id).unlink(missing_ok=True)
        threading.Thread(target=self._validate, args=(r.file_id, data), daemon=True,
                         name=f"hubzoid-file-{r.file_id[:8]}").start()
        return self.describe(row, actor)

    def _validate(self, file_id: str, data: bytes) -> None:
        """The declared type must match the bytes (a renamed executable or an
        HTML page posing as an image is quarantined)."""
        state, problem = "ready", None
        with self.engine.connect() as conn:
            row = self._row(conn, file_id)
        mime = row.mime_type
        head = data[:512].lstrip().lower()
        if mime.startswith("image/") and mime != "image/svg+xml":
            sig = {b"\x89png": "image/png", b"\xff\xd8\xff": "image/jpeg", b"gif8": "image/gif", b"riff": "image/webp"}
            if not any(data[:4].lower().startswith(k) for k in sig):
                state, problem = "quarantined", "The bytes are not the image type they claim to be."
        elif mime == "application/pdf" and not data.startswith(b"%PDF"):
            state, problem = "quarantined", "The bytes are not a PDF."
        elif mime in ("text/plain", "text/csv", "text/markdown", "application/json"):
            if head.startswith((b"<!doctype html", b"<html", b"<script")) or b"\x00" in data[:4096]:
                state, problem = "quarantined", "The bytes are not plain text."
        elif data[:2] == b"MZ" or data[:4] == b"\x7fELF":
            state, problem = "quarantined", "Executable files can't be attached."
        with self.engine.begin() as conn:
            conn.execute(files.update().where(files.c.file_id == file_id, files.c.state == "validating")
                         .values(state=state, processing_error=problem, revision=files.c.revision + 1))
            convs = [c for (c,) in conn.execute(sa.select(references.c.conversation_id).where(
                references.c.file_id == file_id, references.c.conversation_id.is_not(None)))]
            for c in convs:
                g.emit(conn, c, "file.changed", file_id, {"state": state})
        for c in convs:
            self.notify(c)

    def recover(self) -> int:
        """At start: files a stopped process left ``validating`` are checked again."""
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(files).where(files.c.hub_id == self.hub, files.c.state == "validating",
                                                       files.c.created_at < _now() - 5)).all()
        n = 0
        for r in rows:
            try:
                data = self._store(r.storage_config_id)[1].read(r.object_key)
            except st.StorageError:
                with self.engine.begin() as conn:
                    conn.execute(files.update().where(files.c.file_id == r.file_id).values(
                        state="quarantined", processing_error="The stored bytes are missing."))
                continue
            self._validate(r.file_id, data)
            n += 1
        return n

    def abort(self, actor, upload_id: str) -> None:
        with self.engine.begin() as conn:
            r = conn.execute(sa.select(uploads).where(uploads.c.upload_id == upload_id,
                                                      uploads.c.owner_principal_id == actor.id)).first()
            if r is None or r.state != "uploading":
                return
            conn.execute(uploads.update().where(uploads.c.upload_id == upload_id).values(state="cancelled"))
        self._staging(upload_id).unlink(missing_ok=True)

    # -- reading ---------------------------------------------------------------------------------
    def get(self, actor, file_id: str) -> dict:
        with self.engine.connect() as conn:
            return self.describe(self._readable(conn, file_id, actor), actor)

    def list(self, actor, *, conversation_id: str | None = None, limit: int = 50, cursor: str | None = None) -> dict:
        limit = max(1, min(int(limit or 50), 100))
        with self.engine.connect() as conn:
            if conversation_id:
                if not self._readable_conversation(conn, conversation_id, actor):
                    raise Err(404, "not_found", "That conversation was not found.")
                ids = sa.select(references.c.file_id).where(references.c.conversation_id == conversation_id,
                                                            references.c.deleted_at.is_(None))
                q = sa.select(files).where(files.c.file_id.in_(ids))
            else:
                q = sa.select(files).where(files.c.owner_principal_id == actor.id)
            q = q.where(files.c.hub_id == self.hub, files.c.deleted_at.is_(None))
            if cursor:
                try:
                    ts, fid = cursor.split("|", 1)
                    q = q.where(sa.or_(files.c.created_at < float(ts),
                                       sa.and_(files.c.created_at == float(ts), files.c.file_id < fid)))
                except ValueError:
                    raise Err(400, "invalid_request", "That page link is not valid.")
            rows = conn.execute(q.order_by(files.c.created_at.desc(), files.c.file_id.desc()).limit(limit + 1)).all()
        nxt = f"{rows[limit - 1].created_at}|{rows[limit - 1].file_id}" if len(rows) > limit else None
        return {"items": [self.describe(r, actor) for r in rows[:limit]], "next_cursor": nxt}

    def content(self, actor, file_id: str, *, start: int = 0, end: int | None = None) -> tuple[dict, bytes]:
        with self.engine.connect() as conn:
            row = self._readable(conn, file_id, actor)
        if row.state not in ("ready", "quarantined") or (row.state == "quarantined" and row.owner_principal_id != actor.id):
            raise Err(409, "file_processing", "This file isn't ready yet.") if row.state == "validating" else \
                Err(404, "file_not_found", "That file was not found.")
        if start >= row.size_bytes and row.size_bytes:
            raise Err(416, "invalid_range", "That byte range is outside the file.", size=row.size_bytes)
        _, store = self._store(row.storage_config_id)
        try:
            data = store.read(row.object_key, start, end)
        except st.StorageError as exc:
            raise Err(503, exc.code, exc.message, retryable=True)
        return self.describe(row, actor), data

    def preview(self, actor, file_id: str, *, offset: int = 0, limit: int = 200) -> dict:
        from .. import upload_previews as pv

        desc, data = self.content(actor, file_id)
        cap = self.settings()["render"]["max_preview_bytes"]
        mime = desc["mime_type"]
        truncated = len(data) > cap
        if mime == "application/pdf":
            text = pv.pdf_preview(data, pages=None)
        elif mime == "text/csv":
            text = pv.csv_preview(data[:cap])
        elif mime == "application/json":
            text = pv.json_preview(data[:cap])
        elif mime.startswith("text/"):
            text = pv.text_preview(data[:cap], offset=max(0, offset), limit=max(1, min(limit, 200)))
        else:
            raise Err(422, "preview_unsupported", "There is no text preview for this type. Download the original.")
        return {"file_id": file_id, "kind": "text", "text": text, "truncated": truncated}

    # -- references ------------------------------------------------------------------------------
    def _writable_conversation(self, conn, conversation_id: str, actor) -> bool:
        if self.chat is None:
            return False
        conv = conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()
        if conv is None or conv.hub != self.hub:
            return False
        return self.chat.role(conn, dict(conv._mapping), actor) in ("owner", "editor")

    def _add_ref(self, conn, file_id, rtype, resource_id, conversation_id, actor_id, local_name) -> str:
        existing = conn.execute(sa.select(references.c.reference_id).where(
            references.c.file_id == file_id, references.c.resource_type == rtype,
            references.c.resource_id == resource_id, references.c.deleted_at.is_(None))).scalar()
        if existing:
            if local_name:
                conn.execute(references.update().where(references.c.reference_id == existing)
                             .values(local_name=local_name))
            return existing
        rid = str(uuid.uuid4())
        conn.execute(references.insert().values(reference_id=rid, file_id=file_id, resource_type=rtype,
                                                resource_id=resource_id, conversation_id=conversation_id,
                                                local_name=local_name, created_by=actor_id, created_at=_now(),
                                                deleted_at=None))
        if conversation_id:
            g.emit(conn, conversation_id, "file_reference.changed", file_id, {"action": "added"})
        return rid

    def attach(self, actor, file_id: str, conversation_id: str) -> dict:
        """Reuse a readable, ready file in a conversation the person may write
        in: a new reference and the conversation's working copy, no new bytes."""
        from .files import _save, file_part
        from .store import chat_key

        with self.engine.begin() as conn:
            row = self._readable(conn, file_id, actor)
            if row.state != "ready":
                raise Err(409, "file_processing", "This file isn't ready to attach yet.")
            if not self._writable_conversation(conn, conversation_id, actor):
                raise Err(404, "not_found", "That conversation was not found.")
            conv = dict(conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()._mapping)
            existing = conn.execute(sa.select(references.c.local_name).where(
                references.c.file_id == file_id, references.c.resource_type == "conversation",
                references.c.resource_id == conversation_id, references.c.deleted_at.is_(None))).scalar()
        key = chat_key(conv)
        if existing and file_part(self.hub_dir, key, existing) is not None:
            local = existing
        else:
            _, data = self.content(actor, file_id)
            local = _save(self.hub_dir, key, row.original_name, data, row.mime_type)
        with self.engine.begin() as conn:
            rid = self._add_ref(conn, file_id, "conversation", conversation_id, conversation_id, actor.id, local)
        self.notify(conversation_id)
        part = file_part(self.hub_dir, key, local) or {}
        return {**part, "id": file_id, "reference_id": rid}

    def references_of(self, actor, file_id: str) -> list[dict]:
        """Where the file is used. Its owner also sees uses in conversations they
        can't read (someone reused it), without naming them, and may remove them."""
        with self.engine.connect() as conn:
            row = self._readable(conn, file_id, actor)
            owner = row.owner_principal_id == actor.id
            rows = conn.execute(sa.select(references).where(references.c.file_id == file_id,
                                                            references.c.deleted_at.is_(None))).all()
            out = []
            for r in rows:
                if r.conversation_id is None or self._readable_conversation(conn, r.conversation_id, actor):
                    out.append({"reference_id": r.reference_id, "resource_type": r.resource_type,
                                "resource_id": r.resource_id, "conversation_id": r.conversation_id,
                                "created_at": r.created_at})
                elif owner:
                    out.append({"reference_id": r.reference_id, "resource_type": r.resource_type,
                                "resource_id": None, "conversation_id": None, "created_at": r.created_at,
                                "elsewhere": True})
            return out

    def detach(self, actor, file_id: str, reference_id: str) -> None:
        with self.engine.begin() as conn:
            self._readable(conn, file_id, actor)
            r = conn.execute(sa.select(references).where(references.c.reference_id == reference_id,
                                                         references.c.file_id == file_id)).first()
            if r is None or r.deleted_at is not None:
                return
            owner = self._row(conn, file_id).owner_principal_id == actor.id
            if r.conversation_id and not owner and not self._writable_conversation(conn, r.conversation_id, actor):
                raise Err(403, "permission_denied", "Only someone who can write in that conversation removes it.")
            conn.execute(references.update().where(references.c.reference_id == reference_id).values(deleted_at=_now()))
            if r.conversation_id:
                g.emit(conn, r.conversation_id, "file_reference.changed", file_id, {"action": "removed"})

    def delete(self, actor, file_id: str, expected_revision: int | None) -> dict:
        with self.engine.begin() as conn:
            row = self._row(conn, file_id)
            if row is None or row.owner_principal_id != actor.id:
                raise Err(404, "file_not_found", "That file was not found.")
            if row.deleted_at is not None:
                return {"file_id": file_id, "state": "deleted"}
            if expected_revision is not None and expected_revision != row.revision:
                raise Err(409, "revision_conflict", "This file changed. Reload it.", current_revision=row.revision)
            refs = conn.execute(sa.select(references).where(
                references.c.file_id == file_id, references.c.deleted_at.is_(None))).all()
            # The owner's own uses ask them to remove it there first. Someone
            # else's reuse never keeps the owner from deleting their file: those
            # references end with it.
            mine = [r for r in refs if r.conversation_id is None
                    or self._readable_conversation(conn, r.conversation_id, actor)]
            in_use = len(mine)
            used_by_artifact = conn.execute(sa.select(artifact_versions.c.version_id).where(
                artifact_versions.c.file_id == file_id).limit(1)).first()
            if in_use or used_by_artifact:
                raise Err(409, "file_in_use", "This file is still attached somewhere. Remove it there first.",
                          references=in_use)
            for r in refs:
                conn.execute(references.update().where(references.c.reference_id == r.reference_id)
                             .values(deleted_at=_now()))
                if r.conversation_id:
                    g.emit(conn, r.conversation_id, "file_reference.changed", file_id, {"action": "removed"})
            conn.execute(files.update().where(files.c.file_id == file_id).values(
                state="deleted", deleted_at=_now(), revision=row.revision + 1))
        try:
            self._store(row.storage_config_id)[1].delete(row.object_key)
        except st.StorageError:
            log.warning("files: bytes of %s stay for a later cleanup", file_id, exc_info=True)
        return {"file_id": file_id, "state": "deleted"}

    # -- artifacts -------------------------------------------------------------------------------
    def capture_artifacts(self, *, conversation_id: str, branch_id: str | None, actor_id: str, run_id: str | None,
                          message_id: str | None, since: float) -> list[dict]:
        """After a run: the documents it wrote in the conversation's artifacts
        folder become artifact versions (a new artifact, or a new version when
        the bytes changed)."""
        from .. import memory as memlib
        from .store import chat_key

        with self.engine.connect() as conn:
            conv = conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()
        if conv is None:
            return []
        folder = memlib.chat_root(self.hub_dir, chat_key(dict(conv._mapping))) / "artifacts"
        if not folder.is_dir():
            return []
        out = []
        from .. import uploads as uploads_lib

        for path in sorted(folder.iterdir()):
            if not path.is_file() or path.is_symlink() or path.name.startswith(".") or uploads_lib.is_sidecar(path.name):
                continue
            if path.stat().st_mtime < since - 1:
                continue
            data = path.read_bytes()
            if len(data) > self.settings()["limits"]["max_file_bytes"]:
                continue
            mime = uploads_lib.guess_mime(path.name, fallback="application/octet-stream")
            got = self._version(conversation_id=conversation_id, branch_id=branch_id, actor_id=actor_id, title=path.name,
                                mime=mime, data=data, run_id=run_id, message_id=message_id, provenance="generated",
                                base_version_id=None, expected_revision=None)
            if got is not None:
                out.append(got)
        if out:
            self.notify(conversation_id)
        return out

    def _version(self, *, conversation_id, branch_id, actor_id, title, mime, data, run_id, message_id, provenance,
                 base_version_id, expected_revision) -> dict | None:
        digest = sha256(data)
        with self.engine.connect() as conn:
            art = conn.execute(sa.select(artifacts).where(artifacts.c.conversation_id == conversation_id,
                                                          artifacts.c.title == title)).first()
            if art is not None and provenance == "generated":
                cur = conn.execute(sa.select(files.c.content_sha256).select_from(
                    artifact_versions.join(files, files.c.file_id == artifact_versions.c.file_id)).where(
                    artifact_versions.c.version_id == art.current_version_id)).scalar()
                if cur == digest:
                    return None
        f = self.register_bytes(actor_id, title, mime, data, origin="generated", check_type=False)
        t = _now()
        with self.engine.begin() as conn:
            art = conn.execute(sa.select(artifacts).where(artifacts.c.conversation_id == conversation_id,
                                                          artifacts.c.title == title)).first()
            if art is None:
                aid = str(uuid.uuid4())
                conn.execute(artifacts.insert().values(
                    artifact_id=aid, hub_id=self.hub, owner_principal_id=actor_id, conversation_id=conversation_id,
                    branch_id=branch_id, title=title, kind=artifact_kind(title, mime), current_version_id=None,
                    revision=0, created_at=t, updated_at=t))
                art = conn.execute(sa.select(artifacts).where(artifacts.c.artifact_id == aid)).first()
            if expected_revision is not None and expected_revision != art.revision:
                raise Err(409, "artifact_conflict", "Someone saved a newer version. Reload, then edit that one.",
                          current_revision=art.revision)
            if provenance == "edited" and base_version_id != art.current_version_id:
                raise Err(409, "artifact_conflict", "Edit the newest version (reload first).",
                          current_version_id=art.current_version_id)
            n = (conn.execute(sa.select(sa.func.max(artifact_versions.c.ordinal)).where(
                artifact_versions.c.artifact_id == art.artifact_id)).scalar() or 0) + 1
            vid = str(uuid.uuid4())
            conn.execute(artifact_versions.insert().values(
                version_id=vid, artifact_id=art.artifact_id, file_id=f["file_id"], ordinal=n,
                base_version_id=base_version_id or art.current_version_id, author_id=actor_id, run_id=run_id,
                message_id=message_id, provenance=provenance, created_at=t))
            conn.execute(artifacts.update().where(artifacts.c.artifact_id == art.artifact_id).values(
                current_version_id=vid, revision=art.revision + 1, updated_at=t))
            self._add_ref(conn, f["file_id"], "artifact", art.artifact_id, conversation_id, actor_id, None)
            g.emit(conn, conversation_id, "artifact_version.created", art.artifact_id,
                   {"version_id": vid, "ordinal": n, "provenance": provenance, "run_id": run_id})
            return self._artifact_json(conn, art.artifact_id)

    def _artifact_json(self, conn, artifact_id: str) -> dict:
        a = conn.execute(sa.select(artifacts).where(artifacts.c.artifact_id == artifact_id)).first()
        vers = conn.execute(sa.select(artifact_versions, files.c.size_bytes, files.c.mime_type,
                                      files.c.content_sha256).select_from(
            artifact_versions.join(files, files.c.file_id == artifact_versions.c.file_id)).where(
            artifact_versions.c.artifact_id == artifact_id).order_by(artifact_versions.c.ordinal)).all()
        return {"artifact_id": a.artifact_id, "conversation_id": a.conversation_id, "branch_id": a.branch_id,
                "title": a.title, "kind": a.kind, "current_version_id": a.current_version_id, "revision": a.revision,
                "updated_at": a.updated_at,
                "versions": [{"version_id": v.version_id, "ordinal": v.ordinal, "file_id": v.file_id,
                              "base_version_id": v.base_version_id, "author_id": v.author_id, "run_id": v.run_id,
                              "message_id": v.message_id, "provenance": v.provenance, "created_at": v.created_at,
                              "size_bytes": v.size_bytes, "mime_type": v.mime_type,
                              "content_sha256": v.content_sha256} for v in vers]}

    def _artifact(self, conn, artifact_id: str, actor):
        a = conn.execute(sa.select(artifacts).where(artifacts.c.artifact_id == artifact_id,
                                                    artifacts.c.hub_id == self.hub)).first()
        if a is None or not self._readable_conversation(conn, a.conversation_id, actor):
            raise Err(404, "not_found", "That artifact was not found.")
        return a

    def list_artifacts(self, actor, conversation_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            if not self._readable_conversation(conn, conversation_id, actor):
                raise Err(404, "not_found", "That conversation was not found.")
            ids = [r.artifact_id for r in conn.execute(sa.select(artifacts.c.artifact_id).where(
                artifacts.c.conversation_id == conversation_id).order_by(artifacts.c.updated_at.desc()))]
            return [self._artifact_json(conn, i) for i in ids]

    def get_artifact(self, actor, artifact_id: str) -> dict:
        with self.engine.connect() as conn:
            self._artifact(conn, artifact_id, actor)
            return self._artifact_json(conn, artifact_id)

    def version_content(self, actor, artifact_id: str, version_id: str | None) -> tuple[dict, dict, bytes]:
        with self.engine.connect() as conn:
            a = self._artifact(conn, artifact_id, actor)
            vid = version_id or a.current_version_id
            v = conn.execute(sa.select(artifact_versions).where(artifact_versions.c.version_id == vid,
                                                                artifact_versions.c.artifact_id == artifact_id)).first()
            if v is None:
                raise Err(404, "not_found", "That version was not found.")
            row = self._row(conn, v.file_id)
        _, store = self._store(row.storage_config_id)
        art = self.get_artifact(actor, artifact_id)
        return art, {"version_id": v.version_id, "ordinal": v.ordinal, "mime_type": row.mime_type,
                     "name": row.original_name}, store.read(row.object_key)

    def revise(self, actor, artifact_id: str, *, content: bytes, base_version_id: str, expected_revision: int) -> dict:
        with self.engine.connect() as conn:
            a = self._artifact(conn, artifact_id, actor)
            if not self._writable_conversation(conn, a.conversation_id, actor):
                raise Err(403, "permission_denied", "You can read this artifact but not change it.")
            mime = conn.execute(sa.select(files.c.mime_type).select_from(
                artifact_versions.join(files, files.c.file_id == artifact_versions.c.file_id)).where(
                artifact_versions.c.version_id == a.current_version_id)).scalar() or "text/plain"
        if len(content) > self.settings()["limits"]["max_file_bytes"]:
            raise Err(413, "upload_too_large", "That version is too large.")
        out = self._version(conversation_id=a.conversation_id, branch_id=a.branch_id, actor_id=actor.id, title=a.title,
                            mime=mime, data=content, run_id=None, message_id=None, provenance="edited",
                            base_version_id=base_version_id, expected_revision=expected_revision)
        self.notify(a.conversation_id)
        return out

    # -- operations ------------------------------------------------------------------------------
    def doctor(self, *, write_probe: bool = True) -> dict:
        cfg = self.settings()["storage"]
        report = {"adapter": cfg["adapter"], "checks": []}
        try:
            _, store = self._store()
            if write_probe:
                key = f"doctor/{uuid.uuid4().hex}"
                probe = b"hubzoid storage probe " + uuid.uuid4().hex.encode()
                store.put(key, probe)
                back = store.read(key)
                report["checks"].append({"check": "write_read", "ok": back == probe})
                store.delete(key)
                report["checks"].append({"check": "delete", "ok": not store.exists(key)})
        except st.StorageError as exc:
            report["checks"].append({"check": "storage", "ok": False, "code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - the report names the failure
            report["checks"].append({"check": "storage", "ok": False, "code": "storage_unavailable",
                                     "message": type(exc).__name__})
        report["ok"] = bool(report["checks"]) and all(c["ok"] for c in report["checks"])
        return report

    def file_for_local(self, conversation_id: str, local_name: str) -> str | None:
        """The file id behind a conversation's working copy, when it has one."""
        with self.engine.connect() as conn:
            return conn.execute(sa.select(references.c.file_id).where(
                references.c.conversation_id == conversation_id, references.c.local_name == local_name,
                references.c.deleted_at.is_(None))).scalar()


_services: dict[str, FileService] = {}


def service_for(hub_dir: Path, engine, hub: str, chat_engine=None) -> FileService:
    key = str(Path(hub_dir).resolve())
    svc = _services.get(key)
    if svc is None or svc.engine is not engine:
        svc = FileService(hub_dir, engine, hub, chat_engine)
        _services[key] = svc
    elif chat_engine is not None:
        svc.chat = chat_engine
    return svc
