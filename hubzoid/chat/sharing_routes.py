"""Routes for collaborators and snapshot links (``hubzoid.chat.sharing``).

  GET|PUT /api/conversations/{id}/collaborators     members and their revision
  POST  /api/shares/previews                        preview a link (owner)
  POST  /api/shares                                 publish exactly that preview
  GET   /api/shares                                 your links (?scope=hub: all, chat.share.manage)
  PATCH /api/shares/{id}                            narrow audience, stop copying, earlier expiry
  DELETE /api/shares/{id}                           revoke
  GET   /api/shares/{token}                         read a link (public ones without sign-in)
  GET   /api/shares/{token}/export                  the link's projection as JSON
  POST  /api/shares/{token}/copies/previews         what a copy would contain
  POST  /api/shares/{token}/copies                  make your own independent copy
  GET   /api/chat/share-operations/{id}             a copy's result

Links made before 2.0 (one per conversation, ``hz_shares``) stay readable at
``/api/shares/{id}`` and manageable through ``/api/conversations/{id}/share``.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ..control import authz
from . import engine_routes as er
from .common import db, error
from . import sharing
from .sharing import SharingService

PUBLIC_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}


def mount(app: FastAPI, ctx) -> SharingService:
    engine = ctx.engine
    svc = SharingService(ctx.hub_dir, engine.engine, ctx.hub)
    ctx.sharing = svc
    call = er.call

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    async def maybe_actor(request: Request):
        try:
            return await actor_of(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return None, None
            raise

    async def manager(user) -> bool:
        try:
            p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        except Exception:  # noqa: BLE001
            return False
        return p.can("chat.share.manage", ctx.hub)

    @app.get("/api/conversations/{cid}/collaborators")
    async def collaborators(cid: str, request: Request):
        _, actor = await actor_of(request)
        conv, role = await call(engine.readable, cid, actor)
        return await call(svc.collaborators, conv, role)

    @app.put("/api/conversations/{cid}/collaborators")
    async def set_collaborators(cid: str, request: Request):
        _, actor = await actor_of(request, True)
        conv, _ = await call(engine.readable, cid, actor)
        body = await er.read_json(request)
        out = await call(svc.set_collaborators, conv, actor.id, body.get("members"), body.get("expected_revision"))
        engine.notify(cid)
        return out

    @app.post("/api/shares/previews")
    async def preview(request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        conv, _ = await call(engine.readable, str(body.get("conversation_id") or ""), actor)
        return await call(svc.preview, conv, actor.id, body)

    @app.post("/api/shares", status_code=201)
    async def publish(request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.publish, actor.id, body, key=request.headers.get("idempotency-key")),
                            status_code=201)

    @app.get("/api/shares")
    async def list_shares(request: Request):
        user, actor = await actor_of(request)
        if request.query_params.get("scope") == "hub":
            if not await manager(user):
                raise authz.denied("chat.share.manage")
            return {"items": await call(svc.list_shares, owner_id=None)}
        return {"items": await call(svc.list_shares, owner_id=actor.id,
                                    conversation_id=request.query_params.get("conversation_id"))}

    @app.patch("/api/shares/{share_id}")
    async def change(share_id: str, request: Request):
        user, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.change, share_id, actor.id, body, manager=await manager(user))

    @app.delete("/api/shares/{share_id}", status_code=204)
    async def revoke(share_id: str, request: Request):
        user, actor = await actor_of(request, True)
        body = await er.read_json(request)
        await call(svc.revoke, share_id, actor.id, body.get("expected_revision"), manager=await manager(user))
        return Response(status_code=204)

    @app.get("/api/shares/{token}")
    async def read(token: str, request: Request):
        _, actor = await maybe_actor(request)
        try:
            out = await asyncio.to_thread(svc.view, token, actor.id if actor else None)
        except er.g.GraphError as exc:
            if exc.status == 404:
                legacy = await _legacy(ctx, token, actor)
                if legacy is not None:
                    return legacy
                if actor is None:
                    # Signed out: sign in first (the same answer whether or not the link exists).
                    raise error(401, "unauthenticated", "Sign in to open this shared conversation.")
            raise er.http(exc)
        return JSONResponse(out, headers=PUBLIC_HEADERS)

    @app.get("/api/shares/{token}/export")
    async def export(token: str, request: Request):
        _, actor = await maybe_actor(request)
        if not svc.settings()["export_enabled"]:
            raise error(403, "feature_disabled", "Exporting shared conversations is turned off.")
        out = await call(svc.view, token, actor.id if actor else None)
        return Response(json.dumps({"version": 1, "format": "hubzoid-share", **out}, indent=2),
                        media_type="application/json",
                        headers={**PUBLIC_HEADERS, "Content-Disposition": 'attachment; filename="conversation.json"'})

    @app.post("/api/shares/{token}/copies/previews")
    async def copy_preview(token: str, request: Request):
        _, actor = await actor_of(request, True)
        return await call(svc.copy_preview, token, actor.id)

    @app.post("/api/shares/{token}/copies", status_code=201)
    async def make_copy(token: str, request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        out = await call(svc.copy, token, actor.id, actor.email, ctx.model_label, str(body.get("digest") or ""),
                         key=request.headers.get("idempotency-key"))
        return JSONResponse(out, status_code=201)

    @app.get("/api/chat/share-operations/{op_id}")
    async def share_operation(op_id: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.operation, op_id, actor.id)

    return svc


async def _legacy(ctx, share_id: str, actor):
    """A pre-2.0 link: signed-in people of the deployment only."""
    from ..auth import users
    from .shares import _disabled_import

    if actor is None or not 8 <= len(share_id) <= 64:
        return None
    share = await db(ctx.store.get_share, share_id)
    if share is None or _disabled_import(share) or await db(users.get, ctx.hub_dir, share["owner_id"]) is None:
        return None
    if not await db(sharing.owner_active, ctx.store.engine, ctx.hub_dir, share["owner_id"]):
        return None
    snap = share.get("snapshot") or {}
    # 1.x snapshots kept reasoning and the answer's signed download links.
    msgs = [{**m, "content": [p for p in m.get("content") or []
                              if not (isinstance(p, dict) and p.get("type") == "reasoning")]}
            for m in snap.get("messages") or []]
    msgs, _ = sharing.unlink_messages(msgs)
    return {"title": snap.get("title"), "agent": snap.get("agent"), "owner_name": snap.get("owner_name"),
            "created_at": snap.get("created_at"), "messages": msgs, "kind": "legacy"}
