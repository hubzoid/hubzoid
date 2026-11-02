"""A conversation's attachments: names, the uploads folder, downloads.

  GET  /api/conversations/{id}/files/{file}     the file (the conversation's owner only)
  POST /api/conversations/{id}/files            410: upload to POST /api/chat/files instead

Uploads go to ``POST /api/chat/files`` (``chat.file_routes``), and the message
names the returned file id; ``hubzoid.attachments.attach`` then puts the file
in the conversation's own uploads folder (``memory.chat_upload_dir`` of its
``store.chat_key``, the chat scope of its runs), which ``read_upload`` and
image vision read. ``{file}`` is the store id, or the file's name in the folder
(messages stored before the store id was the file id).

The body is capped while it is received (``settings.max_upload_bytes`` per file,
25 MiB by default), not after, so an oversized upload never fills the disk.
Downloads show raster images inline; everything else is an attachment, and
active content (HTML, SVG, XML, scripts) is never served with its own type.
"""
from __future__ import annotations

import itertools
import os
import secrets
import unicodedata
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from .. import memory as memlib
from .. import uploads as uploads_lib
from .common import ChatContext, db, error, not_found
from ..attachments import RASTER_IMAGES
from .store import chat_key

# Multipart framing around the one file (boundaries, part headers, small fields).
_MULTIPART_SLACK = 64 * 1024
_NAME_MAX = 120
# Reserved on some file systems, plus the brackets that frame the attachment
# notes the agent reads ([Image: name], [User attached file: ...]).
_FORBIDDEN_CHARS = set('\\/:*?"<>|[]')
_ACTIVE_MARKERS = ("html", "xml", "javascript", "ecmascript", "svg")


def too_large(limit: int):
    mib = limit / (1024 * 1024)
    size = f"{mib:.0f} MiB" if mib >= 1 else f"{limit} bytes"
    return error(413, "file_too_large", f"That file is larger than the {size} upload limit.")


def safe_upload_name(raw: str | None) -> str:
    """A file name safe on every file system and for the agent's tools: no
    directories, control characters, reserved characters or leading dots, at
    most 120 characters (keeping the extension)."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN_CHARS)
    name = " ".join(name.split()).lstrip(".").strip()
    if name.endswith(uploads_lib.SIDECAR_SUFFIX):
        name = name[: -len(uploads_lib.SIDECAR_SUFFIX)] + "-file"
    if not name:
        name = "upload"
    if len(name) > _NAME_MAX:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            name = stem[: _NAME_MAX - len(ext) - 1].rstrip() + "." + ext
        else:
            name = name[:_NAME_MAX]
    return name


def _split(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if dot and stem and 0 < len(ext) <= 16:
        return stem, "." + ext
    return name, ""


def reserve_name(upload_dir: Path, name: str) -> str:
    """Claim ``name`` in the folder, or ``name-2``, ``name-3`` ... when taken.
    Creating the file exclusively makes concurrent uploads of one name safe."""
    stem, ext = _split(name)

    def numbered(suffix: str) -> str:
        # Shorten the stem so the suffix fits the 120-character name limit
        # that later reads enforce (HUB-140 F29).
        room = _NAME_MAX - len(ext) - len(suffix)
        return f"{stem[:room].rstrip()}{suffix}{ext}"

    candidates = itertools.chain(
        (name if n == 1 else numbered(f"-{n}") for n in range(1, 1000)),
        (numbered(f"-{secrets.token_hex(4)}") for _ in range(10)))
    for candidate in candidates:
        if (upload_dir / f"{candidate}{uploads_lib.SIDECAR_SUFFIX}").exists():
            continue
        try:
            fd = os.open(upload_dir / candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise error(409, "name_unavailable", "Could not store the file under its name. Rename it and try again.")


def kind_of(mime: str) -> str:
    return "image" if mime in RASTER_IMAGES else "file"


def _mime(content_type: str | None, name: str) -> str:
    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        mime = uploads_lib.guess_mime(name, fallback=mime or "application/octet-stream")
    return mime


def upload_dir(hub_dir: Path, key: str) -> Path:
    """The uploads folder of the conversation whose ``store.chat_key`` is ``key``."""
    return memlib.chat_upload_dir(Path(hub_dir), key)


def stored_file(hub_dir: Path, key: str, file_id: str) -> Path | None:
    """The stored file for ``file_id`` in the conversation whose
    ``store.chat_key`` is ``key``, or None."""
    if not isinstance(file_id, str) or not file_id or safe_upload_name(file_id) != file_id:
        return None
    if uploads_lib.is_sidecar(file_id):
        return None
    base = memlib.chat_root(Path(hub_dir), key) / "uploads"   # never created here
    target = base / file_id
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != base.resolve() or not resolved.is_file():
        return None
    return target


def file_part(hub_dir: Path, key: str, file_id: str) -> dict | None:
    """The stored content part for an uploaded file (from the server's own
    metadata, never the client's), or None when there is no such file."""
    target = stored_file(hub_dir, key, file_id)
    if target is None:
        return None
    meta = uploads_lib.read_meta(target.parent, file_id) or {}
    mime = str(meta.get("mime") or uploads_lib.guess_mime(file_id))
    try:
        size = int(meta.get("size"))
    except (TypeError, ValueError):
        size = target.stat().st_size
    return {"type": kind_of(mime), "file_id": file_id, "name": file_id, "mime": mime, "size": size}


def _capped_receive(receive, cap: int, limit: int):
    received = 0

    async def wrapped():
        nonlocal received
        message = await receive()
        if message.get("type") == "http.request":
            received += len(message.get("body") or b"")
            if received > cap:
                raise too_large(limit)
        return message

    return wrapped


async def _read_capped(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _is_active(mime: str) -> bool:
    return any(marker in mime for marker in _ACTIVE_MARKERS)


async def receive_one(request: Request, limit: int) -> tuple[str, str, bytes]:
    """(safe name, type, bytes) of the one multipart ``file``, capped while it
    is received. Raises HTTPException (400, 413)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + _MULTIPART_SLACK:
        raise too_large(limit)
    capped = Request(request.scope, _capped_receive(request.receive, limit + _MULTIPART_SLACK, limit))
    try:
        form = await capped.form(max_files=1, max_fields=8)
    except Exception as exc:  # noqa: BLE001 — a malformed body is the client's error
        if getattr(exc, "status_code", None) == 413:
            raise
        raise error(400, "invalid_upload", "The upload could not be read. Try again.") from exc
    try:
        item = form.get("file")
        if not isinstance(item, UploadFile):
            raise error(400, "file_required", "Choose a file to upload (multipart field 'file').")
        payload = await _read_capped(item, limit)
        name = safe_upload_name(item.filename)
        mime = _mime(item.content_type, name)
    finally:
        await form.close()
    return name, mime, payload


def register(app: FastAPI, ctx: ChatContext) -> None:
    @app.post("/api/conversations/{conv_id}/files")
    async def upload_file_gone(conv_id: str):
        raise error(410, "upload_route_gone", "Upload each file to POST /api/chat/files, then send its file_id "
                                              "in the message.")

    @app.get("/api/conversations/{conv_id}/files/{file_id}")
    async def download_file(conv_id: str, file_id: str, request: Request):
        user = await ctx.user(request)
        conv = await ctx.owned(user, conv_id)
        ctx.this_hub(conv)
        key = chat_key(conv)
        part = await db(file_part, ctx.hub_dir, key, file_id)
        svc = getattr(ctx, "files", None)
        if part is None and svc is not None:
            # A store id: the conversation's own copy of that file.
            local = await db(svc.local_for, conv_id, file_id)
            part = await db(file_part, ctx.hub_dir, key, local) if local else None
        if part is None:
            raise not_found("file")
        target = stored_file(ctx.hub_dir, key, part["name"])
        if target is None:
            raise not_found("file")
        mime = part["mime"]
        inline = part["type"] == "image"
        media_type = "application/octet-stream" if _is_active(mime) else mime
        return FileResponse(
            str(target), media_type=media_type, filename=part["name"],
            content_disposition_type="inline" if inline else "attachment",
            headers={"X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "sandbox; default-src 'none'",
                     "Cache-Control": "private, max-age=300"})
