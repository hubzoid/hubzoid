"""Routes for Workspace authoring (``hubzoid.chat.authoring``).

  GET    /api/chat/authoring/files                       authorable hub files and their hashes
  GET    /api/chat/authoring/files/versions?path=        published versions of one file
  POST   /api/chat/authoring/drafts                      {path, content?} -> a draft (from the current file)
  GET    /api/chat/authoring/drafts[/{id}]
  PATCH  /api/chat/authoring/drafts/{id}                 {content, expected_revision}
  POST   /api/chat/authoring/drafts/{id}/submit          {expected_revision} -> in review (valid only)
  POST   /api/chat/authoring/drafts/{id}/review          {decision, digest, note?} (authoring.publish)
  POST   /api/chat/authoring/drafts/{id}/publish         {digest} -> one atomic write; stale base refused
  POST   /api/chat/authoring/rollbacks                   {path, version_id} -> a draft restoring that version
  POST   /api/chat/authoring/openapi/preview             {spec, selected?} -> operations; tools for the selected
  GET    /api/chat/prompts                               shared and your own prompts
  PUT    /api/chat/prompts/{id}                          {content, expected_revision}: your own prompt
  POST   /api/chat/prompts/{id}/render                   {scope, values} -> text for the composer (not sent)
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request

from ..control import authz
from . import engine_routes as er
from .authoring import AuthoringService, service_for


def mount(app: FastAPI, ctx) -> AuthoringService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub)
    ctx.authoring = svc
    call = er.call
    A = "/api/chat/authoring"

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    async def need(user, capability: str) -> None:
        p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        if not p.can(capability, ctx.hub):
            raise authz.denied(capability)

    @app.get(f"{A}/files")
    async def files(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        return {"items": await call(svc.files)}

    @app.get(f"{A}/files/versions")
    async def versions(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        return {"items": await call(svc.versions, request.query_params.get("path") or "")}

    @app.post(f"{A}/drafts", status_code=201)
    async def create(request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.create_draft, actor, str(body.get("path") or ""), body.get("content"))

    @app.get(f"{A}/drafts")
    async def list_drafts(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        return {"items": await call(svc.list_drafts)}

    @app.get(f"{A}/drafts/{{did}}")
    async def get_draft(did: str, request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        return await call(svc.get_draft, did)

    @app.patch(f"{A}/drafts/{{did}}")
    async def update(did: str, request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.update_draft, actor, did, str(body.get("content") or ""), body.get("expected_revision"))

    @app.post(f"{A}/drafts/{{did}}/submit")
    async def submit(did: str, request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request)
        return await call(svc.submit, actor, did, body.get("expected_revision"))

    @app.post(f"{A}/drafts/{{did}}/review")
    async def review(did: str, request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.publish")
        body = await er.read_json(request)
        return await call(svc.review, actor, did, decision=str(body.get("decision") or ""),
                          digest=str(body.get("digest") or ""), note=body.get("note"))

    @app.post(f"{A}/drafts/{{did}}/publish")
    async def publish(did: str, request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.publish")
        body = await er.read_json(request)
        out = await call(svc.publish, actor, did, str(body.get("digest") or ""))
        if out["path"].endswith(("AGENTS.md", "SKILL.md")) or out["path"].startswith("skills/"):
            out["runtime_reloaded"] = await _reload_runtime(ctx)
        return out

    @app.post(f"{A}/rollbacks", status_code=201)
    async def rollback(request: Request):
        user, actor = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request)
        return await call(svc.rollback_draft, actor, str(body.get("path") or ""), str(body.get("version_id") or ""))

    @app.post(f"{A}/openapi/preview")
    async def openapi(request: Request):
        user, _ = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.openapi_preview, body.get("spec"), body.get("selected"))

    @app.get("/api/chat/prompts")
    async def prompts(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.prompts, actor)}

    @app.put("/api/chat/prompts/{pid}")
    async def put_prompt(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.put_personal_prompt, actor, pid, str(body.get("content") or ""),
                          body.get("expected_revision"))

    @app.post("/api/chat/prompts/{pid}/render")
    async def render(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.render, actor, pid, str(body.get("scope") or "hub"), body.get("values") or {})

    return svc


async def _reload_runtime(ctx) -> bool:
    """New chat replies use the published instructions and skills at once: the
    chat's runtime is rebuilt from the hub's files. Replies already running keep
    the runtime they started with. (Other surfaces of this bridge, such as Open
    WebUI, Slack and MCP, pick the change up when the hub restarts.)"""
    import logging

    from .. import runtime as runtime_lib

    try:
        new = await asyncio.to_thread(runtime_lib.build, ctx.hub_dir)
        opener = getattr(new, "aopen", None)
        if opener is not None:
            await opener()
    except Exception:  # noqa: BLE001 - the file is published; the next restart applies it
        logging.getLogger("hubzoid.chat.authoring").warning("authoring: runtime rebuild failed", exc_info=True)
        return False
    ctx.runtime = new
    if getattr(ctx, "models", None) is not None:
        ctx.models.runtime = new
    return True
