"""Routes for the model catalog and presets (``hubzoid.chat.models``).

  GET    /api/chat/models               the catalog you may pick from, and whether you may compare
  GET    /api/chat/presets              your presets and the hub's shared ones
  PUT    /api/chat/presets/{id}         {expected_revision, name, model_ref | candidate_refs, parameters, default}
  DELETE /api/chat/presets/{id}

Comparing is the ``compare`` operation on ``/api/conversations/{id}/operations``.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import engine_routes as er
from .models import ModelService, service_for


def mount(app: FastAPI, ctx) -> ModelService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub, ctx.runtime)
    ctx.models = svc
    engine.models = svc
    call = er.call

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    @app.get("/api/chat/models")
    async def models(request: Request):
        _, actor = await actor_of(request)
        return await call(svc.describe, actor.email)

    @app.get("/api/chat/presets")
    async def list_presets(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.list_presets, actor)}

    @app.put("/api/chat/presets/{preset_id}")
    async def put_preset(preset_id: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.put_preset, actor, preset_id, body, body.get("expected_revision"))

    @app.delete("/api/chat/presets/{preset_id}", status_code=204)
    async def delete_preset(preset_id: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.delete_preset, actor, preset_id)
        return Response(status_code=204)

    return svc
