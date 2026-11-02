"""Attachments have one path (``proposals/2026-11-02-attachments-one-path.md``).

Two steps, used by every surface:

* ``save`` is the only function that stores a file. It checks the size and
  that the bytes are the type they claim (no executables or Office macros),
  stores them once in the hub's file store and returns the file's record (id,
  name, type, size, SHA-256, owner).
* ``attach`` is the only function that puts a stored file in a chat. It checks
  the caller may read the file, adds the chat's reference and makes the file
  appear in the chat's uploads folder (``memory.chat_upload_dir``) under a safe
  unique name, as a hard link to the stored bytes (a copy only across disks).
  It returns the message part: ``{"type", "file_id", "name", "mime", "size"}``,
  where ``file_id`` is the store id and ``name`` the file's name in the folder.

The agent is told one way: images go to the runtime as a list of names
(``_request_ctx.image_scope``, ``SessionSlot.images``), and every other file
gets ``note`` and is read with ``read_upload``. The web app and the API send
the file ids they got from ``POST /api/chat/files``; Slack, Telegram and
WhatsApp upload there too (each runs as its own process, so only the bridge
writes the store); the terminal, email intake and Sources call ``save``.

Old messages stored before this change carry ``file_id`` equal to the folder
name, and keep working: ``part_name`` falls back to it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import memory as memlib
from . import uploads as uploads_lib

# Pictures the models take directly. Other image types (SVG, TIFF, HEIC ...)
# are offered to the agent as files.
RASTER_IMAGES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: The owner of files a channel (Slack, Telegram, WhatsApp) uploads with a bridge key.
BRIDGE_OWNER = "bridge"


class AttachmentError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def service(hub_dir: Path):
    """The hub's file store (``chat.file_service``): the bridge's own when it
    is running in this process, else one opened on the deployment's engine."""
    from .chat import file_service as fs

    svc = fs._services.get(str(Path(hub_dir).resolve()))
    if svc is not None:
        return svc
    from .intake.service import files_for

    return files_for(Path(hub_dir))


def kind_of(mime: str) -> str:
    return "image" if mime in RASTER_IMAGES else "file"


def save(hub_dir: Path, data: bytes, name: str, mime: str, owner: str, *, origin: str = "upload",
         limit: int | None = None) -> dict:
    """Store ``data`` once and return its record (``file_service.describe``).
    Raises ``chat.graph.GraphError`` (413 too large, 415 type not allowed or
    bytes that are not their claimed type)."""
    from .chat.file_service import Err, content_problem
    from .chat.files import safe_upload_name

    from .chat.file_service import DEFAULT_TYPES, MACRO_TYPES

    svc = service(hub_dir)
    name = safe_upload_name(name)
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = uploads_lib.guess_mime(name, fallback=mime or "application/octet-stream")
    limits = svc.settings()["limits"]
    cap = min(limits["max_file_bytes"], limit) if limit else limits["max_file_bytes"]
    if len(data) > cap:
        size = f"{cap // 1048576} MiB" if cap >= 1048576 else f"{cap} bytes"
        raise Err(413, "upload_too_large", f"That file is larger than the {size} limit.", limit=cap)
    # Any type, as chat always took, except Office files with macros and
    # executables; a deployment that sets its own type list restricts to it.
    allowed = limits["allowed_mime_types"]
    if mime in MACRO_TYPES or (sorted(allowed) != sorted(DEFAULT_TYPES) and mime not in allowed):
        raise Err(415, "type_not_allowed", f"Files of type {mime} can't be attached here.", allowed=allowed)
    problem = content_problem(mime, data)
    if problem:
        raise Err(415, "type_mismatch", problem)
    return svc.register_bytes(owner, name, mime, data, origin=origin, check_type=False)


def _link(src: Path, folder: Path, name: str) -> str:
    """Make ``src`` appear in ``folder`` under a unique safe name: a hard link
    (the same bytes, so deleting it from the store leaves the chat readable),
    or a copy when the two folders are on different disks."""
    from .chat.files import reserve_name

    final = reserve_name(folder, name)
    target = folder / final
    target.unlink(missing_ok=True)   # reserve_name claims it with an empty file
    try:
        os.link(src, target)
    except OSError:
        shutil.copyfile(src, target)
    return final


def _readable_row(svc, conn, file_id, *, actor=None, owner: str | None = None):
    row = svc._row(conn, file_id) if isinstance(file_id, str) and file_id else None
    ok = row is not None and row.deleted_at is None and (
        (owner is not None and row.owner_principal_id == owner)
        or (actor is not None and svc._may_read(conn, row, actor)))
    return row if ok else None


def readable(hub_dir: Path, file_id: str, *, actor=None, owner: str | None = None) -> bool:
    """Whether the caller may attach ``file_id`` (a check before a conversation exists)."""
    svc = service(hub_dir)
    with svc.engine.connect() as conn:
        row = _readable_row(svc, conn, file_id, actor=actor, owner=owner)
    return row is not None and row.state == "ready"


def attach(hub_dir: Path, file_id: str, chat_key: str, *, actor=None, owner: str | None = None,
           conversation_id: str | None = None) -> dict:
    """Put the stored file ``file_id`` in the chat whose scope is ``chat_key``
    and return its message part. The caller must be able to read it: ``actor``
    (a chat engine actor, checked by the store's references) or ``owner`` (the
    principal id that saved it, for bridge-key and in-process callers).
    Attaching the same file to the same chat again returns the same part."""
    import sqlalchemy as sa

    from .chat import file_service as fs

    svc = service(hub_dir)
    with svc.engine.connect() as conn:
        row = _readable_row(svc, conn, file_id, actor=actor, owner=owner)
        if row is None:
            raise AttachmentError(400, "file_not_found",
                                  f"The attached file {file_id!r} was not found. Upload it again.")
        if row.state != "ready":
            raise AttachmentError(409, "file_processing", "This file isn't ready to attach yet.")
        resource = conversation_id or chat_key
        rtype = "conversation" if conversation_id else "chat"
        existing = conn.execute(sa.select(fs.references.c.local_name).where(
            fs.references.c.file_id == file_id, fs.references.c.resource_type == rtype,
            fs.references.c.resource_id == resource, fs.references.c.deleted_at.is_(None))).scalar()
    folder = memlib.chat_upload_dir(Path(hub_dir), chat_key)
    local = existing if existing and (folder / existing).is_file() else None
    if local is None:
        _, store = svc._store(row.storage_config_id)
        src = getattr(store, "_path", None)
        if src is not None and src(row.object_key).is_file():
            local = _link(src(row.object_key), folder, row.original_name)
        else:
            try:
                data = store.read(row.object_key)
            except Exception as exc:  # noqa: BLE001 - storage_unavailable / object_missing
                raise AttachmentError(503, getattr(exc, "code", "storage_unavailable"),
                                      getattr(exc, "message", "The stored bytes of this file are unavailable."))
            from .chat.files import reserve_name

            local = reserve_name(folder, row.original_name)
            (folder / local).write_bytes(data)
        meta = {"mime": row.mime_type, "size": row.size_bytes,
                "kind": uploads_lib.classify(row.mime_type, (folder / local).read_bytes())}
        import json

        (folder / f"{local}{uploads_lib.SIDECAR_SUFFIX}").write_text(json.dumps(meta), encoding="utf-8")
    created_by = getattr(actor, "id", None) or owner or row.owner_principal_id
    with svc.engine.begin() as conn:
        svc._add_ref(conn, file_id, rtype, resource, conversation_id, created_by, local)
    if conversation_id:
        svc.notify(conversation_id)
    return {"type": kind_of(row.mime_type), "file_id": file_id, "name": local,
            "mime": row.mime_type, "size": row.size_bytes}


def vision_on() -> bool:
    """``HUBZOID_VISION`` (on by default): images are shown to the model. Off,
    an image gets the same note as any other file."""
    from .settings import truthy

    return truthy(os.environ.get("HUBZOID_VISION", "true"))


def attach_conversation(hub_dir: Path, actor, file_id: str, conversation_id: str) -> dict:
    """``attach`` to a native conversation the actor may write in (reuse from
    the web app or ``hubzoid files attach``). Raises AttachmentError."""
    import sqlalchemy as sa

    from .chat.store import chat_key, conversations

    svc = service(hub_dir)
    with svc.engine.connect() as conn:
        if not svc._writable_conversation(conn, conversation_id, actor):
            raise AttachmentError(404, "not_found", "That conversation was not found.")
        conv = dict(conn.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).first()._mapping)
    return attach(hub_dir, file_id, chat_key(conv), actor=actor, conversation_id=conversation_id)


def part_name(part: dict) -> str:
    """The file's name in the chat folder: ``name``, or ``file_id`` for parts
    stored before the store id was the file id."""
    return str(part.get("name") or part.get("file_id") or "")


def note(name: str, size: int, mime: str, target: Path) -> str:
    """The one note that tells the agent about a non-image attachment, or a
    missing one. An image is named here and shown to the model as an image block."""
    if not target.is_file():
        return (f"[Attachment unreadable: the user attached {name} but it is no longer "
                f"available. Tell them and ask them to upload it again. Do NOT guess its "
                f"contents.]")
    if mime in RASTER_IMAGES and vision_on():
        return f"[Image: {name}]  (attached image, shown to you directly)"
    # The on-disk path is for path-accepting tools and hub scripts.
    return (f"[User attached file: {name} ({size} bytes, {mime}). "
            f"Read it with read_upload('{name}'), or pass its on-disk path to a "
            f"path-accepting tool or script: {target}]")


def describe_parts(hub_dir: Path, chat_key: str, parts: list) -> tuple[list[str], list[str]]:
    """(notes, image names) for a message's file and image parts, in order."""
    folder = memlib.chat_upload_dir(Path(hub_dir), chat_key)
    notes: list[str] = []
    images: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict) or part.get("type") not in ("file", "image"):
            continue
        name = part_name(part)
        if not name:
            continue
        mime = str(part.get("mime") or uploads_lib.guess_mime(name))
        target = folder / name
        notes.append(note(name, int(part.get("size") or 0), mime, target))
        if mime in RASTER_IMAGES and target.is_file() and vision_on():
            images.append(name)
    return notes, images
