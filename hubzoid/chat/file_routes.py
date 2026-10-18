"""Routes for files and artifacts (``hubzoid.chat.file_service``).

  POST   /api/chat/files/uploads                    start a resumable upload (Idempotency-Key)
  PUT    /api/chat/files/uploads/{id}/bytes         one chunk at Upload-Offset (Upload-Chunk-SHA256)
  POST   /api/chat/files/uploads/{id}/complete      {content_sha256} -> the file (validating)
  DELETE /api/chat/files/uploads/{id}               cancel
  GET    /api/chat/files[?conversation_id=]         your files, or a conversation's
  GET    /api/chat/files/{id}                       one file's descriptor
  GET    /api/chat/files/{id}/content               the bytes (Range; attachment unless a safe image)
  GET    /api/chat/files/{id}/preview               a text preview
  GET    /api/chat/files/{id}/references            where it is used (that you can see)
  DELETE /api/chat/files/{id}/references/{ref}      remove one use (bytes stay while others use it)
  DELETE /api/chat/files/{id}                       delete (only when nothing uses it)
  POST   /api/conversations/{id}/files/reuse        {file_id}: attach a file you can read, no new bytes
  POST   /api/chat/files/doctor                     storage write probe (hub managers)
  GET    /api/chat/artifacts?conversation_id=       a conversation's artifacts and versions
  GET    /api/chat/artifacts/{id}
  GET    /api/chat/artifacts/{id}/versions/{v}/content
  POST   /api/chat/artifacts/{id}/versions          {content, base_version_id, expected_revision}
  GET    /api/chat/artifact-frame                   the sandboxed viewer frame (no network)
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..control import authz
from . import engine_routes as er
from .common import error
from .file_service import CHUNK_BYTES, FileService, service_for

SAFE_INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp"}

#: The generated document's own policy: scripts and styles inline, media from
#: data/blob only, no network, no frames, no forms.
CHILD_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
             "media-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; "
             "base-uri 'none'")
#: The wrapper frame: its only child is a blob: document it made itself, so a
#: generated page can't navigate itself (or anything) to a remote address.
FRAME_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src blob:; "
             "child-src blob:; img-src data: blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

FRAME_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview</title>
<style>html,body{margin:0;height:100%;background:#fff}iframe{border:0;width:100%;height:100%;display:block}</style>
</head><body><iframe id="f" sandbox="allow-scripts" referrerpolicy="no-referrer" title="Preview"></iframe>
<script>
(function () {
  var frame = document.getElementById("f"), url = null;
  var meta = '<meta http-equiv="Content-Security-Policy" content="__CSP__">';
  function wrap(kind, source) {
    if (kind === "svg") return "<!doctype html><html><head>" + meta + "</head><body style=\\"margin:0\\">" + source + "</body></html>";
    var m = /^\\s*<!doctype[^>]*>/i.exec(source);
    return m ? m[0] + meta + source.slice(m[0].length) : "<!doctype html>" + meta + source;
  }
  window.addEventListener("message", function (e) {
    if (e.source !== window.parent) return;
    var d = e.data || {};
    if (d.type !== "hz-render" || typeof d.source !== "string") return;
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(new Blob([wrap(d.kind, d.source)], { type: "text/html" }));
    frame.src = url;
  });
  window.parent.postMessage({ type: "hz-frame-ready" }, "*");
})();
</script></body></html>""".replace("__CSP__", CHILD_CSP)


def _range(header: str | None, size: int) -> tuple[int, int | None] | None:
    if not header:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise error(416, "invalid_range", "That byte range is not valid.")
    if not m.group(1):
        n = int(m.group(2))
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start > end or start >= size:
        raise error(416, "invalid_range", "That byte range is outside the file.")
    return start, min(end, size - 1)


def _bytes_response(desc: dict, data: bytes, *, inline: bool, rng: tuple[int, int | None] | None) -> Response:
    mime = desc["mime_type"]
    show = inline and mime in SAFE_INLINE
    name = quote(desc.get("name") or "file")
    headers = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'",
               "Cache-Control": "private, no-store", "ETag": f'"{desc.get("content_sha256", "")}"',
               "Accept-Ranges": "bytes", "Referrer-Policy": "no-referrer",
               "Content-Disposition": f"{'inline' if show else 'attachment'}; filename*=UTF-8''{name}"}
    media = mime if show else "application/octet-stream"
    if rng is not None:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{desc['size_bytes']}"
        return Response(data, status_code=206, media_type=media, headers=headers)
    return Response(data, media_type=media, headers=headers)


def mount(app: FastAPI, ctx) -> FileService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub, engine)
    ctx.files = svc
    svc.notify = lambda cid: engine.notify(cid)  # the current chain (search indexing joins it later)
    call = er.call

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    # -- uploads ---------------------------------------------------------------------------
    @app.post("/api/chat/files/uploads", status_code=201)
    async def start_upload(request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.start_upload, actor, body, key=request.headers.get("idempotency-key")),
                            status_code=201)

    @app.put("/api/chat/files/uploads/{upload_id}/bytes")
    async def put_bytes(upload_id: str, request: Request):
        _, actor = await actor_of(request, True)
        raw = request.headers.get("upload-offset", "")
        if not raw.isdigit():
            raise error(400, "invalid_request", "Send the chunk's position as Upload-Offset.")
        chunks, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > CHUNK_BYTES:
                raise error(413, "chunk_too_large", f"Send at most {CHUNK_BYTES} bytes per chunk.")
            chunks.append(part)
        return await call(svc.put_chunk, actor, upload_id, int(raw), b"".join(chunks),
                          request.headers.get("upload-chunk-sha256"))

    @app.post("/api/chat/files/uploads/{upload_id}/complete", status_code=202)
    async def complete(upload_id: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.complete, actor, upload_id, body.get("content_sha256")), status_code=202)

    @app.delete("/api/chat/files/uploads/{upload_id}", status_code=204)
    async def cancel(upload_id: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.abort, actor, upload_id)
        return Response(status_code=204)

    # -- files -------------------------------------------------------------------------------
    @app.get("/api/chat/files")
    async def list_files(request: Request):
        _, actor = await actor_of(request)
        q = request.query_params
        return await call(svc.list, actor, conversation_id=q.get("conversation_id"),
                          limit=int(q.get("limit") or 50), cursor=q.get("cursor"))

    @app.get("/api/chat/files/{file_id}")
    async def get_file(file_id: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.get, actor, file_id)

    @app.get("/api/chat/files/{file_id}/content")
    async def content(file_id: str, request: Request):
        _, actor = await actor_of(request)
        desc = await call(svc.get, actor, file_id)
        rng = _range(request.headers.get("range"), desc["size_bytes"])
        desc, data = await call(svc.content, actor, file_id, start=rng[0] if rng else 0, end=rng[1] if rng else None)
        return _bytes_response(desc, data, inline=request.query_params.get("disposition") == "inline", rng=rng)

    @app.get("/api/chat/files/{file_id}/preview")
    async def preview(file_id: str, request: Request):
        _, actor = await actor_of(request)
        q = request.query_params
        return await call(svc.preview, actor, file_id, offset=int(q.get("offset") or 0), limit=int(q.get("limit") or 200))

    @app.get("/api/chat/files/{file_id}/references")
    async def refs(file_id: str, request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.references_of, actor, file_id)}

    @app.delete("/api/chat/files/{file_id}/references/{ref_id}", status_code=204)
    async def detach(file_id: str, ref_id: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.detach, actor, file_id, ref_id)
        return Response(status_code=204)

    @app.delete("/api/chat/files/{file_id}")
    async def delete_file(file_id: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.delete, actor, file_id, body.get("expected_revision")), status_code=202)

    @app.post("/api/conversations/{cid}/files/reuse", status_code=201)
    async def reuse(cid: str, request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.attach, actor, str(body.get("file_id") or ""), cid), status_code=201)

    @app.post("/api/chat/files/doctor")
    async def doctor(request: Request):
        user, _ = await actor_of(request, True)
        p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        if not p.can("admin.diagnostics.run", ctx.hub):
            raise authz.denied("admin.diagnostics.run")
        body = await er.read_json(request)
        return await asyncio.to_thread(svc.doctor, write_probe=body.get("write_probe", True) is not False)

    # -- artifacts ---------------------------------------------------------------------------
    @app.get("/api/chat/artifacts")
    async def list_artifacts(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.list_artifacts, actor, request.query_params.get("conversation_id") or "")}

    @app.get("/api/chat/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.get_artifact, actor, artifact_id)

    @app.get("/api/chat/artifacts/{artifact_id}/versions/{version_id}/content")
    async def version_content(artifact_id: str, version_id: str, request: Request):
        _, actor = await actor_of(request)
        art, ver, data = await call(svc.version_content, actor, artifact_id, version_id)
        desc = {"mime_type": ver["mime_type"], "name": ver["name"], "size_bytes": len(data),
                "content_sha256": next((v["content_sha256"] for v in art["versions"]
                                        if v["version_id"] == ver["version_id"]), "")}
        if request.query_params.get("as") == "text":
            # The viewer reads a version's source as text and renders it itself,
            # inside the sandboxed frame; it is never served as a page here.
            return JSONResponse({"version": ver, "text": data.decode("utf-8", "replace")},
                                headers={"Cache-Control": "private, no-store"})
        return _bytes_response(desc, data, inline=request.query_params.get("disposition") == "inline", rng=None)

    @app.post("/api/chat/artifacts/{artifact_id}/versions", status_code=201)
    async def revise(artifact_id: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        text = body.get("content")
        if not isinstance(text, str):
            raise error(422, "validation_failed", "content is the new version's text.")
        return JSONResponse(await call(svc.revise, actor, artifact_id, content=text.encode(),
                                       base_version_id=body.get("base_version_id"),
                                       expected_revision=body.get("expected_revision")), status_code=201)

    @app.get("/api/chat/artifact-frame")
    async def artifact_frame():
        return HTMLResponse(FRAME_PAGE, headers={"Content-Security-Policy": FRAME_CSP, "X-Content-Type-Options": "nosniff",
                                                 "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"})

    return svc
