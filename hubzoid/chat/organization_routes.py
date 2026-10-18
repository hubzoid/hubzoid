"""Routes for organisation, search and portability (HUB-73).

  GET  /api/chat/organization                  your folders, tags, placements, bookmarks
  POST /api/chat/organization/apply            manifest: dry run (digest), then apply that digest
  GET|POST /api/chat/folders, PATCH|DELETE /api/chat/folders/{id}
  GET  /api/chat/folders/{id}/effective-context
  GET|POST /api/chat/tags, DELETE /api/chat/tags/{id}
  GET|POST /api/chat/bookmarks, DELETE /api/chat/bookmarks/{id}
  PATCH /api/conversations/{id}/organization   folder, pin, archive, tags (yours only)
  POST /api/chat/organization/bulk             one action on up to bulk_limit conversations
  GET  /api/chat/search                        full text, filtered by access before ranking
  GET  /api/chat/search/status, POST /api/chat/search/rebuild
  POST /api/chat/exports                       a hubzoid-exchange package of chosen conversations
  GET  /api/chat/exports/{id}                  download it
  POST /api/chat/imports/preview               upload (multipart ``file``): objects and digest
  POST /api/chat/imports                       apply a reviewed preview
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import engine_routes as er
from . import portability as port
from .common import error
from .organization import OrganizationService
from .search import SearchService


def mount(app: FastAPI, ctx) -> None:
    engine = ctx.engine
    org = OrganizationService(engine.engine, ctx.hub, ctx.hub_dir)
    search = SearchService(engine.engine, ctx.hub, ctx.hub_dir)
    imports = port.ImportStore(ctx.hub_dir)
    ctx.organization, ctx.search = org, search
    call = er.call
    wake = engine.notify

    def notify(conversation_id: str) -> None:
        wake(conversation_id)
        search.schedule(conversation_id)

    engine.notify = notify

    def initial_index() -> None:
        try:
            if search.status()["documents"] == 0:
                search.rebuild()
        except Exception:  # noqa: BLE001 - search reports itself as lagging; chat goes on
            import logging

            logging.getLogger("hubzoid.chat.search").warning("search: first index failed", exc_info=True)

    import threading

    threading.Thread(target=initial_index, name="hz-search-index", daemon=True).start()

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return await asyncio.to_thread(engine.actor, user)

    def readable_for(actor):
        return lambda cid: engine.readable(cid, actor)

    @app.get("/api/chat/organization")
    async def get_org(request: Request):
        return await call(org.export, (await actor_of(request)).id)

    @app.post("/api/chat/organization/apply")
    async def apply_org(request: Request):
        actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(org.apply_manifest, actor.id, body.get("manifest"), dry_run=bool(body.get("dry_run", True)),
                          digest=body.get("digest"), readable=readable_for(actor))

    @app.get("/api/chat/folders")
    async def folders_list(request: Request):
        return {"items": (await call(org.export, (await actor_of(request)).id))["folders"]}

    @app.post("/api/chat/folders", status_code=201)
    async def folders_create(request: Request):
        actor = await actor_of(request, True)
        return JSONResponse(await call(org.create_folder, actor.id, await er.read_json(request)), status_code=201)

    @app.patch("/api/chat/folders/{fid}")
    async def folders_update(fid: str, request: Request):
        actor = await actor_of(request, True)
        return await call(org.update_folder, actor.id, fid, await er.read_json(request))

    @app.delete("/api/chat/folders/{fid}", status_code=204)
    async def folders_delete(fid: str, request: Request):
        actor = await actor_of(request, True)
        await call(org.delete_folder, actor.id, fid, await er.read_json(request))
        return Response(status_code=204)

    @app.get("/api/chat/folders/{fid}/effective-context")
    async def folder_context(fid: str, request: Request):
        actor = await actor_of(request)
        return await call(org.resolve_context, actor.id, fid)

    @app.get("/api/chat/tags")
    async def tags_list(request: Request):
        return {"items": (await call(org.export, (await actor_of(request)).id))["tags"]}

    @app.post("/api/chat/tags", status_code=201)
    async def tags_create(request: Request):
        actor = await actor_of(request, True)
        return JSONResponse(await call(org.create_tag, actor.id, await er.read_json(request)), status_code=201)

    @app.delete("/api/chat/tags/{tid}", status_code=204)
    async def tags_delete(tid: str, request: Request):
        actor = await actor_of(request, True)
        body = await er.read_json(request)
        await call(org.delete_tag, actor.id, tid, body.get("expected_revision"))
        return Response(status_code=204)

    @app.get("/api/chat/bookmarks")
    async def bookmarks_list(request: Request):
        return {"items": (await call(org.export, (await actor_of(request)).id))["bookmarks"]}

    @app.post("/api/chat/bookmarks", status_code=201)
    async def bookmarks_create(request: Request):
        actor = await actor_of(request, True)
        out = await call(org.add_bookmark, actor.id, await er.read_json(request), readable=readable_for(actor))
        return JSONResponse(out, status_code=201)

    @app.delete("/api/chat/bookmarks/{bid}", status_code=204)
    async def bookmarks_delete(bid: str, request: Request):
        actor = await actor_of(request, True)
        body = await er.read_json(request)
        await call(org.delete_bookmark, actor.id, bid, body.get("expected_revision"))
        return Response(status_code=204)

    @app.patch("/api/conversations/{cid}/organization")
    async def organize(cid: str, request: Request):
        actor = await actor_of(request, True)
        return await call(org.organize, actor.id, cid, await er.read_json(request), readable=readable_for(actor))

    @app.post("/api/chat/organization/bulk")
    async def bulk(request: Request):
        actor = await actor_of(request, True)
        return await call(org.bulk, actor.id, await er.read_json(request), readable=readable_for(actor))

    @app.get("/api/chat/search")
    async def do_search(request: Request):
        actor = await actor_of(request)
        q = request.query_params
        types = [t for t in (q.get("types") or "").split(",") if t] or None
        tags = [t for t in (q.get("tag_ids") or "").split(",") if t] or None
        pinned = {"true": True, "false": False}.get(q.get("pinned") or "")
        try:
            limit = int(q.get("limit")) if q.get("limit") else None
        except ValueError:
            raise error(400, "invalid_request", "limit is a number.")
        return await call(search.search, actor, q.get("q") or "", types=types, limit=limit, cursor=q.get("cursor"),
                          archived=q.get("archived") or "exclude", folder_id=q.get("folder_id"), tag_ids=tags,
                          pinned=pinned)

    @app.get("/api/chat/search/status")
    async def search_status(request: Request):
        await actor_of(request)
        return await call(search.status)

    @app.post("/api/chat/search/rebuild", status_code=202)
    async def search_rebuild(request: Request):
        from ..control import authz

        user = await ctx.mutation(request)
        p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        if not p.can("admin.diagnostics.run", ctx.hub):
            raise authz.denied("admin.diagnostics.run")
        return await call(search.rebuild)

    exports: dict[str, tuple[str, bytes]] = {}

    @app.post("/api/chat/exports", status_code=201)
    async def make_export(request: Request):
        actor = await actor_of(request, True)
        body = await er.read_json(request)
        ids = body.get("resource_ids") or []
        if not isinstance(ids, list) or not ids or len(ids) > 1000:
            raise error(422, "validation_failed", "resource_ids lists 1 to 1,000 conversations.")
        orgs = await call(org.export, actor.id)
        data = await call(port.export_package, engine.engine, ctx.hub_dir, actor_id=actor.id, conversation_ids=ids,
                          readable=readable_for(actor), organization=orgs)
        eid = str(uuid.uuid4())
        while len(exports) >= 20:  # kept in memory briefly, for the download that follows
            exports.pop(next(iter(exports)))
        exports[eid] = (actor.id, data)
        return JSONResponse({"id": eid, "revision": 1, "state": "complete", "bytes": len(data),
                             "download_url": f"/api/chat/exports/{eid}"}, status_code=201)

    @app.get("/api/chat/exports/{eid}")
    async def get_export(eid: str, request: Request):
        actor = await actor_of(request)
        got = exports.get(eid)
        if got is None or got[0] != actor.id:
            raise error(404, "not_found", "That export was not found.")
        return Response(got[1], media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="hubzoid-export.zip"', "Cache-Control": "no-store"})

    @app.post("/api/chat/imports/preview")
    async def import_preview(request: Request):
        actor = await actor_of(request, True)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > port.MAX_UPLOAD + 1024 * 1024:
            raise error(413, "request_too_large", "That file is too large to import.")
        # The form parser spools file parts to disk; only a checked size is read in.
        form = await request.form(max_files=1, max_fields=4)
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise error(422, "validation_failed", "Upload the export as the multipart field 'file'.")
        if upload.size is not None and upload.size > port.MAX_UPLOAD:
            raise error(413, "request_too_large", "That file is too large to import.")
        data = await upload.read(port.MAX_UPLOAD + 1)
        if len(data) > port.MAX_UPLOAD:
            raise error(413, "request_too_large", "That file is too large to import.")
        fmt, records, blobs, problems = await call(port.parse, data)
        objects = port.preview(records, problems, blobs)
        meta = await asyncio.to_thread(imports.save, actor.id, data, fmt, objects)
        return {"id": meta["id"], "revision": 1, "digest": port.digest_with(meta, {}), "format": fmt,
                "objects": objects, "errors": [o for o in objects if o["disposition"] == "reject"]}

    @app.post("/api/chat/imports", status_code=201)
    async def import_apply(request: Request):
        actor = await actor_of(request, True)
        body = await er.read_json(request)
        meta, data = await call(imports.load, str(body.get("preview_id") or ""), actor.id)
        dispositions = body.get("dispositions") or {}
        if not isinstance(dispositions, dict):
            raise error(422, "validation_failed", "dispositions maps type:source_id to a choice.")
        proposed = {f"{o['type']}:{o['source_id']}": o["disposition"] for o in meta["objects"]}
        for k, v in dispositions.items():
            if v not in ("import", "skip", "quarantine", "reject") or k not in proposed:
                raise error(422, "validation_failed", f"Unknown object or choice for {k!r}.")
            if proposed[k] == "reject" and v == "import":
                raise error(422, "validation_failed", f"{k} is corrupt or incomplete and can't be imported.")
        if port.digest_with(meta, dispositions) != body.get("digest"):
            raise error(409, "revision_conflict", "Apply exactly the preview and choices you reviewed.")
        if "result" in meta:
            return JSONResponse(meta["result"], status_code=200)
        fmt, records, blobs, _problems = await call(port.parse, data)
        effective = {**proposed, **dispositions}
        await ctx.may_use_hub(SimpleNamespace(email=actor.email))
        result = await call(port.apply, engine.engine, ctx.hub_dir, ctx.hub, actor_id=actor.id,
                            actor_email=actor.email, agent=ctx.model_label, records=records, blobs=blobs,
                            dispositions=effective, operation_id=str(uuid.uuid4()), key=f"import:{meta['id']}",
                            request={"digest": body.get("digest"), "dispositions": dispositions})
        await asyncio.to_thread(imports.done, meta["id"], result)
        for cid in result["conversations"].values():
            search.schedule(cid)
        return JSONResponse(result, status_code=201)
