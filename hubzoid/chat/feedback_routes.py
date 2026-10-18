"""Routes for feedback, helpers and notifications (``hubzoid.chat.feedback``).

  PUT|GET|DELETE /api/conversations/{id}/messages/{mid}/feedback   your rating of an answer
  GET  /api/chat/feedback[/export]          ratings of this hub (chat.feedback.read)
  POST /api/chat/helpers                    start a helper (follow-ups, title)
  GET  /api/chat/helpers                    your helper results for a conversation
  PATCH /api/conversations/{id}/title       rename (a typed title always wins)
  GET  /api/chat/notifications              your inbox
  PATCH /api/chat/notifications/{id}        read or dismiss
  POST /api/chat/notification-presence      you are looking at a conversation
  GET  /api/chat/notification-targets       targets of a scope (own|hub)
  PUT|DELETE /api/chat/notification-targets/{id}
  POST /api/chat/notification-targets/{id}/verify
  GET  /api/chat/notification-deliveries    a target's delivery history
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response

from ..control import authz
from . import engine_routes as er
from .common import db, error
from .feedback import FeedbackService, service_for
from .graph import GraphError

log = logging.getLogger("hubzoid.chat.feedback")


def mount(app: FastAPI, ctx) -> FeedbackService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub)
    ctx.feedback = svc
    call = er.call
    deliverer = _Deliverer(svc)

    def on_terminal(**kw):
        try:
            if svc.on_run_terminal(**kw):
                deliverer.kick()
        except Exception:  # noqa: BLE001 - a notice never breaks the reply
            log.warning("chat: could not record the notice for run %s", kw.get("run_id"), exc_info=True)

    engine.on_terminal = on_terminal

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    async def manager(user, capability: str) -> bool:
        try:
            p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        except Exception:  # noqa: BLE001
            return False
        return p.can(capability, ctx.hub)

    # -- feedback ------------------------------------------------------------------
    @app.put("/api/conversations/{cid}/messages/{mid}/feedback")
    async def put_feedback(cid: str, mid: str, request: Request):
        _, actor = await actor_of(request, True)
        conv, _role = await call(engine.readable, cid, actor)
        body = await er.read_json(request)
        return await call(svc.put_feedback, conv, actor.id, mid, body)

    @app.get("/api/conversations/{cid}/messages/{mid}/feedback")
    async def get_feedback(cid: str, mid: str, request: Request):
        _, actor = await actor_of(request)
        await call(engine.readable, cid, actor)
        return {"feedback": await call(svc.get_feedback, actor.id, mid)}

    @app.delete("/api/conversations/{cid}/messages/{mid}/feedback", status_code=204)
    async def delete_feedback(cid: str, mid: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(engine.readable, cid, actor)
        body = await er.read_json(request)
        await call(svc.delete_feedback, actor.id, mid, body.get("expected_revision"))
        return Response(status_code=204)

    @app.get("/api/chat/feedback")
    async def list_feedback(request: Request):
        user, _ = await actor_of(request)
        if not await manager(user, "chat.feedback.read"):
            raise authz.denied("chat.feedback.read")
        q = request.query_params
        return await call(svc.list_feedback, agent_id=q.get("agent_id"), rating=q.get("rating"),
                          reason=q.get("reason"), limit=int(q.get("limit") or 50), cursor=q.get("cursor"))

    @app.get("/api/chat/feedback/export")
    async def export_feedback(request: Request):
        user, _ = await actor_of(request)
        if not await manager(user, "chat.feedback.read"):
            raise authz.denied("chat.feedback.read")
        fmt = request.query_params.get("format") or "json"
        if fmt not in ("json", "csv"):
            raise error(422, "validation_failed", "format is json or csv.")
        data, media = await call(svc.export_feedback, fmt, agent_id=request.query_params.get("agent_id"),
                                 rating=request.query_params.get("rating"))
        return Response(data, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="feedback.{fmt}"', "Cache-Control": "no-store"})

    # -- helpers -------------------------------------------------------------------
    @app.post("/api/chat/helpers", status_code=202)
    async def start_helper(request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        conv, _role = await call(engine.readable, str(body.get("conversation_id") or ""), actor)
        job = await call(svc.start_helper, conv, actor.id, str(body.get("kind") or ""),
                         str(body.get("branch_id") or ""), body.get("input_revision"))
        if job["state"] == "queued":
            asyncio.ensure_future(asyncio.to_thread(svc.run_helper, job["id"]))
        return job

    @app.get("/api/chat/helpers")
    async def list_helpers(request: Request):
        _, actor = await actor_of(request)
        cid = request.query_params.get("conversation_id") or ""
        await call(engine.readable, cid, actor)
        return {"items": await call(svc.list_helpers, cid, request.query_params.get("branch_id"), actor.id)}

    @app.patch("/api/conversations/{cid}/title")
    async def set_title(cid: str, request: Request):
        user, actor = await actor_of(request, True)
        conv, role = await call(engine.readable, cid, actor)
        if role != "owner":
            raise error(403, "permission_denied", "Only the owner can rename this conversation.")
        body = await er.read_json(request)
        title = " ".join(str(body.get("title") or "").split())
        if not title or len(title) > 200:
            raise error(422, "validation_failed", "A title is 1 to 200 characters.")
        await db(ctx.store.update_conversation, cid, title=title, title_source="user")
        return {"id": cid, "title": title, "source": "user"}

    # -- notifications -------------------------------------------------------------
    @app.get("/api/chat/notifications")
    async def notifications(request: Request):
        _, actor = await actor_of(request)

        def readable(cid: str) -> bool:
            try:
                engine.readable(cid, actor)
                return True
            except Exception:  # noqa: BLE001
                return False

        return await call(svc.notifications, actor.id, readable=readable)

    @app.patch("/api/chat/notifications/{item_id}")
    async def mark(item_id: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.mark, actor.id, item_id, str(body.get("state") or ""))

    @app.post("/api/chat/notification-presence")
    async def presence(request: Request):
        _, actor = await actor_of(request, True)
        return await call(svc.presence, actor.id, await er.read_json(request))

    async def scope_key(request: Request, user, actor) -> str:
        scope = request.query_params.get("scope") or "own"
        is_manager = await manager(user, "chat.notifications.manage") if scope == "hub" else False
        try:
            return svc.scope_of(scope, actor.id, manager=is_manager)
        except GraphError as exc:
            raise er.http(exc)

    @app.get("/api/chat/notification-targets")
    async def list_targets(request: Request):
        user, actor = await actor_of(request)
        key = await scope_key(request, user, actor)
        return await call(svc.list_targets, key)

    @app.put("/api/chat/notification-targets/{target_id}")
    async def put_target(target_id: str, request: Request):
        user, actor = await actor_of(request, True)
        key = await scope_key(request, user, actor)
        body = await er.read_json(request)
        owner = actor.id if key.startswith("user:") else None
        return await call(svc.put_target, key, target_id, body, owner_id=owner,
                          expected_revision=body.get("expected_revision", "absent"))

    @app.delete("/api/chat/notification-targets/{target_id}", status_code=204)
    async def delete_target(target_id: str, request: Request):
        user, actor = await actor_of(request, True)
        key = await scope_key(request, user, actor)
        body = await er.read_json(request)
        await call(svc.delete_target, key, target_id, body.get("expected_revision"))
        return Response(status_code=204)

    @app.post("/api/chat/notification-targets/{target_id}/verify")
    async def verify_target(target_id: str, request: Request):
        user, actor = await actor_of(request, True)
        key = await scope_key(request, user, actor)
        return await call(svc.verify_target, key, target_id)

    @app.get("/api/chat/notification-deliveries")
    async def delivery_history(request: Request):
        user, actor = await actor_of(request)
        key = await scope_key(request, user, actor)
        return {"items": await call(svc.delivery_history, key, request.query_params.get("target_id") or "")}

    return svc


class _Deliverer:
    """Sends due webhook deliveries in the background while there are any."""

    def __init__(self, svc: FeedbackService):
        self.svc = svc
        self._lock = threading.Lock()
        self._running = False

    def kick(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._loop, name="hz-notify", daemon=True).start()

    def _loop(self) -> None:
        idle = 0
        try:
            while idle < 3:
                try:
                    sent = self.svc.deliver_due()
                except Exception:  # noqa: BLE001
                    log.warning("chat: delivery pass failed", exc_info=True)
                    sent = 0
                idle = 0 if sent else idle + 1
                time.sleep(2)
        finally:
            with self._lock:
                self._running = False

