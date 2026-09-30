"""Attachments: upload a file into a conversation, download it again.

  POST /api/conversations/{id}/files            multipart ``file`` -> 201 {file_id, name, size, mime, kind}
  GET  /api/conversations/{id}/files/{file_id}  the file (the conversation's owner only)

Files are written with ``uploads.write_with_meta`` into the conversation's own
uploads folder (``memory.chat_upload_dir``), the same store ``read_upload`` and
image vision read, so the agent sees them with no copy. ``file_id`` is the
stored file name: the uploaded name made safe and unique in the folder, which is
also the name the agent is told.

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
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile

from .. import memory as memlib
from .. import uploads as uploads_lib
from .common import ChatContext, db, error, not_found
from .history import RASTER_IMAGES

# Multipart framing around the one file (boundaries, part headers, small fields).
_MULTIPART_SLACK = 64 * 1024
_NAME_MAX = 120
_FORBIDDEN_CHARS = set('\\/:*?"<>|')
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
    candidates = itertools.chain(
        (name if n == 1 else f"{stem}-{n}{ext}" for n in range(1, 1000)),
        (f"{stem}-{secrets.token_hex(4)}{ext}" for _ in range(10)))
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


def upload_dir(hub_dir: Path, conversation_id: str) -> Path:
    return memlib.chat_upload_dir(Path(hub_dir), conversation_id)


def stored_file(hub_dir: Path, conversation_id: str, file_id: str) -> Path | None:
    """The stored file for ``file_id`` in this conversation, or None."""
    if not isinstance(file_id, str) or not file_id or safe_upload_name(file_id) != file_id:
        return None
    if uploads_lib.is_sidecar(file_id):
        return None
    base = upload_dir(hub_dir, conversation_id)
    target = base / file_id
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved.parent != base.resolve() or not resolved.is_file():
        return None
    return target


def file_part(hub_dir: Path, conversation_id: str, file_id: str) -> dict | None:
    """The stored content part for an uploaded file (from the server's own
    metadata, never the client's), or None when there is no such file."""
    target = stored_file(hub_dir, conversation_id, file_id)
    if target is None:
        return None
    meta = uploads_lib.read_meta(target.parent, file_id) or {}
    mime = str(meta.get("mime") or uploads_lib.guess_mime(file_id))
    try:
        size = int(meta.get("size"))
    except (TypeError, ValueError):
        size = target.stat().st_size
    return {"type": kind_of(mime), "file_id": file_id, "name": file_id, "mime": mime, "size": size}


def _save(hub_dir: Path, conversation_id: str, name: str, payload: bytes, mime: str) -> str:
    folder = upload_dir(hub_dir, conversation_id)
    final = reserve_name(folder, name)
    try:
        uploads_lib.write_with_meta(folder, final, payload, mime=mime)
    except BaseException:
        (folder / final).unlink(missing_ok=True)
        raise
    return final


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


def register(app: FastAPI, ctx: ChatContext) -> None:
    @app.post("/api/conversations/{conv_id}/files", status_code=201)
    async def upload_file(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        ctx.this_hub(conv)
        await ctx.may_use_hub(user)
        limit = int(ctx.settings.max_upload_bytes)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit + _MULTIPART_SLACK:
            raise too_large(limit)
        capped = Request(request.scope, _capped_receive(request.receive,
                                                        limit + _MULTIPART_SLACK, limit))
        try:
            form = await capped.form(max_files=1, max_fields=8)
        except Exception as exc:  # noqa: BLE001 — a malformed body is the client's error
            if getattr(exc, "status_code", None) == 413:
                raise
            raise error(400, "invalid_upload", "The upload could not be read. Try again.") from exc
        try:
            item = form.get("file")
            if not isinstance(item, UploadFile):
                raise error(400, "file_required", "Choose a file to upload.")
            payload = await _read_capped(item, limit)
            name = safe_upload_name(item.filename)
            mime = _mime(item.content_type, name)
        finally:
            await form.close()
        stored = await db(_save, ctx.hub_dir, conv["id"], name, payload, mime)
        return JSONResponse(status_code=201, content={
            "file_id": stored, "name": stored, "size": len(payload), "mime": mime,
            "kind": kind_of(mime)})

    @app.get("/api/conversations/{conv_id}/files/{file_id}")
    async def download_file(conv_id: str, file_id: str, request: Request):
        user = await ctx.user(request)
        conv = await ctx.owned(user, conv_id)
        ctx.this_hub(conv)
        part = await db(file_part, ctx.hub_dir, conv["id"], file_id)
        if part is None:
            raise not_found("file")
        target = stored_file(ctx.hub_dir, conv["id"], file_id)
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
