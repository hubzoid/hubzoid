"""Routes for sources and retrieval (``hubzoid.chat.retrieval``).

  GET    /api/chat/retrieval/collections            collections you can read
  GET    /api/chat/retrieval/collections/{id}
  PUT    /api/chat/retrieval/collections/{id}       {expected_revision, manifest}
  DELETE /api/chat/retrieval/collections/{id}
  POST   /api/chat/retrieval/collections/{id}/jobs  {operation: "sync"} -> job
  GET    /api/chat/retrieval/jobs/{id}
  POST   /api/chat/retrieval/queries                {query, collection_ids?, top_k?} -> passages and citations
  GET    /api/chat/retrieval/citations/{id}         the cited passage, re-authorised now
  POST   /api/chat/retrieval/web/fetch              {url} -> a public page's title and text (checked fetch)
  GET    /api/chat/retrieval/health
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import engine_routes as er
from .retrieval import RetrievalService, extract, service_for


def mount(app: FastAPI, ctx) -> RetrievalService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub, getattr(ctx, "files", None))
    ctx.retrieval = svc
    call = er.call
    P = "/api/chat/retrieval"

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    @app.get(f"{P}/collections")
    async def list_collections(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.list, actor)}

    @app.get(f"{P}/collections/{{cid}}")
    async def get_collection(cid: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.get, actor, cid)

    @app.put(f"{P}/collections/{{cid}}")
    async def put_collection(cid: str, request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        manifest = dict(body.get("manifest") or {})
        manifest["id"] = cid
        return await call(svc.apply, actor, manifest, expected_revision=body.get("expected_revision"))

    @app.delete(f"{P}/collections/{{cid}}", status_code=204)
    async def delete_collection(cid: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.delete, actor, cid)
        return Response(status_code=204)

    @app.post(f"{P}/collections/{{cid}}/jobs", status_code=202)
    async def start_job(cid: str, request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        if body.get("operation", "sync") not in ("sync", "reindex"):
            raise er.http(er.g.GraphError(422, "validation_failed", "operation is sync or reindex."))
        return JSONResponse(await call(svc.start_sync, actor, cid), status_code=202)

    @app.get(f"{P}/jobs/{{jid}}")
    async def get_job(jid: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.job, actor, jid)

    @app.post(f"{P}/queries")
    async def query(request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        return await call(svc.query, actor, str(body.get("query") or ""), collection_ids=body.get("collection_ids"),
                          top_k=body.get("top_k"), agent=body.get("agent_id"),
                          conversation_id=body.get("conversation_id"))

    @app.get(f"{P}/citations/{{cit}}")
    async def citation(cit: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.citation, actor, cit)

    @app.post(f"{P}/web/fetch")
    async def web_fetch(request: Request):
        user, _ = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        web = svc.settings()["web"]

        def run():
            final, mime, data = svc.fetch(str(body.get("url") or ""), web["limits"], allowed=web["allowed_domains"],
                                          blocked=web["blocked_domains"])
            title, pages = extract(final, mime, data)
            text = "\n".join(t for _p, t in pages)
            return {"url": final, "title": title, "mime_type": mime, "bytes": len(data), "excerpt": text[:2000]}

        return await call(run)

    @app.get(f"{P}/health")
    async def health(request: Request):
        await actor_of(request)
        cfg = svc.settings()
        dialect = engine.engine.dialect.name
        return {"enabled": cfg["enabled"], "search": "fts5-bm25" if dialect == "sqlite" else "postgres-fts",
                "extractor": "builtin (text, markdown, csv, json, html, pdf)", "vector_store": None,
                "web": {"enabled": cfg["web"]["enabled"], "search": cfg["web"]["search"],
                        "native_search": {"claude": "WebSearch (server-side)", "codex": "not enabled",
                                          "openai": "not enabled"}}}

    return svc
