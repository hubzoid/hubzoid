"""Routes for spaces, notes and the calendar (``hubzoid.chat.spaces``).

  GET    /api/chat/spaces                            your channels and direct messages
  POST   /api/chat/spaces                            {name, kind: channel|dm, people, open}
  PUT    /api/chat/spaces/{id}/members               {people, expected_revision} (owner)
  POST   /api/chat/spaces/{id}/mute                  {muted}
  GET    /api/chat/spaces/{id}/posts[?parent_id=]    posts (or a thread), and pinned posts
  POST   /api/chat/spaces/{id}/posts                 {body, parent_id?}; "@agent" asks the agent in the thread
  PATCH  /api/chat/posts/{id}                        {body, expected_revision} (your own)
  DELETE /api/chat/posts/{id}
  POST   /api/chat/posts/{id}/reactions              {emoji, on}
  POST   /api/chat/posts/{id}/pin                    {on}
  GET    /api/chat/spaces/{id}/events                SSE: wakes when the space changes
  GET    /api/chat/notes[?space_id=]  POST /api/chat/notes
  PUT    /api/chat/notes/{id}                        {title, body, expected_revision}; stale -> 409 with theirs
  GET    /api/chat/notes/{id}/revisions  POST /api/chat/notes/{id}/restore {revision, expected_revision}
  DELETE /api/chat/notes/{id}
  GET    /api/chat/calendar?start=&end=              occurrences you can see
  POST   /api/chat/calendar  PUT|DELETE /api/chat/calendar/{id}
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import engine_routes as er
from .spaces import SpacesService, service_for

log = logging.getLogger("hubzoid.chat.spaces")
REMINDER_SECONDS = 20.0


def mount(app: FastAPI, ctx) -> SpacesService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub)
    ctx.spaces = svc
    call = er.call
    bus = er.EventBus()
    svc.changed = lambda sid: bus.wake(f"space:{sid}")
    feedback = getattr(ctx, "feedback", None)
    if feedback is not None:
        svc.notify = feedback.notify
        feedback.resolvers["space"] = svc.resolve_ref
        feedback.resolvers["event"] = svc.resolve_ref
    _start_reminders(svc)

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        await ctx.may_use_hub(user)  # spaces belong to this hub: hub use first, on every request
        return user, await asyncio.to_thread(engine.actor, user)

    @app.get("/api/chat/spaces")
    async def list_spaces(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.list_spaces, actor)}

    @app.post("/api/chat/spaces", status_code=201)
    async def create_space(request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.create_space, actor, name=str(body.get("name") or ""),
                                       kind=body.get("kind") or "channel", people=body.get("people") or [],
                                       open_=bool(body.get("open"))), status_code=201)

    @app.put("/api/chat/spaces/{sid}/members")
    async def set_members(sid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.set_members, actor, sid, body.get("people") or [], body.get("expected_revision"))

    @app.post("/api/chat/spaces/{sid}/mute")
    async def mute(sid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.mute, actor, sid, bool(body.get("muted")))

    @app.get("/api/chat/spaces/{sid}/posts")
    async def list_posts(sid: str, request: Request):
        _, actor = await actor_of(request)
        return await call(svc.posts, actor, sid, parent_id=request.query_params.get("parent_id"))

    @app.post("/api/chat/spaces/{sid}/posts", status_code=201)
    async def create_post(sid: str, request: Request):
        user, actor = await actor_of(request, True)
        await ctx.may_use_hub(user)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        post, ask_agent = await call(svc.post, actor, sid, str(body.get("body") or ""), parent_id=body.get("parent_id"))
        if ask_agent:
            # The agent answers in the thread, as the person who asked (their identity and grants).
            thread = post["parent_id"] or post["id"]
            pending = await asyncio.to_thread(svc.agent_reply_start, sid, thread, actor.id)
            asyncio.ensure_future(_agent_reply(ctx, svc, sid, thread, pending, actor))
        return JSONResponse(post, status_code=201)

    @app.patch("/api/chat/posts/{pid}")
    async def edit_post(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.edit, actor, pid, str(body.get("body") or ""), body.get("expected_revision"))

    @app.delete("/api/chat/posts/{pid}", status_code=204)
    async def delete_post(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.delete_post, actor, pid)
        return Response(status_code=204)

    @app.post("/api/chat/posts/{pid}/reactions")
    async def react(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.react, actor, pid, str(body.get("emoji") or ""), body.get("on", True) is not False)

    @app.post("/api/chat/posts/{pid}/pin")
    async def pin(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.pin, actor, pid, body.get("on", True) is not False)

    @app.get("/api/chat/spaces/{sid}/events")
    async def space_events(sid: str, request: Request):
        _, actor = await actor_of(request)
        await call(svc.posts, actor, sid, parent_id=None)  # readable now
        key = f"space:{sid}"
        q = bus.subscribe(key)

        async def stream():
            try:
                yield b"retry: 3000\n\n"
                while True:
                    try:
                        await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    if await request.is_disconnected():
                        return
                    try:  # still a reader?
                        await asyncio.to_thread(svc.posts, actor, sid, parent_id=None, limit=1)
                    except er.g.GraphError:
                        yield b'data: {"type": "access.revoked"}\n\n'
                        return
                    yield b'data: {"type": "space.changed"}\n\n'
            finally:
                bus.unsubscribe(key, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- notes ---------------------------------------------------------------------------------
    @app.get("/api/chat/notes")
    async def list_notes(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.list_notes, actor, request.query_params.get("space_id"))}

    @app.post("/api/chat/notes", status_code=201)
    async def create_note(request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return JSONResponse(await call(svc.create_note, actor, str(body.get("title") or ""), str(body.get("body") or ""),
                                       body.get("space_id")), status_code=201)

    @app.put("/api/chat/notes/{nid}")
    async def save_note(nid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.save_note, actor, nid, title=str(body.get("title") or ""), body=str(body.get("body") or ""),
                          expected_revision=body.get("expected_revision"))

    @app.get("/api/chat/notes/{nid}/revisions")
    async def note_revisions(nid: str, request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.note_revisions, actor, nid)}

    @app.post("/api/chat/notes/{nid}/restore")
    async def restore_note(nid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.restore_note, actor, nid, int(body.get("revision") or 0), body.get("expected_revision"))

    @app.delete("/api/chat/notes/{nid}", status_code=204)
    async def delete_note(nid: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.delete_note, actor, nid)
        return Response(status_code=204)

    # -- calendar ------------------------------------------------------------------------------
    @app.get("/api/chat/calendar")
    async def calendar(request: Request):
        _, actor = await actor_of(request)
        q = request.query_params
        start = float(q.get("start") or time.time() - 86400)
        end = float(q.get("end") or start + 14 * 86400)
        if end - start > 366 * 86400:
            raise er.http(er.g.GraphError(422, "validation_failed", "Ask for at most a year at a time."))
        return {"items": await call(svc.list_events, actor, start, end)}

    @app.post("/api/chat/calendar", status_code=201)
    async def create_event(request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return JSONResponse(await call(svc.save_event, actor, body), status_code=201)

    @app.put("/api/chat/calendar/{eid}")
    async def update_event(eid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.save_event, actor, body, event_id=eid)

    @app.delete("/api/chat/calendar/{eid}", status_code=204)
    async def delete_event(eid: str, request: Request):
        _, actor = await actor_of(request, True)
        await call(svc.delete_event, actor, eid)
        return Response(status_code=204)

    return svc


async def _agent_reply(ctx, svc: SpacesService, sid: str, thread: str, pending: str, actor) -> None:
    """The agent's answer in a thread, run as the person who mentioned it."""
    from .. import access, run_events
    from .runs import web_identity

    try:
        text = await asyncio.to_thread(svc.thread_text, sid, thread)
        prompt = ("You were mentioned with @agent in a team channel thread. Answer the latest question in the "
                  "thread, briefly, for the whole channel.\n\n" + text)
        identity = await asyncio.to_thread(web_identity, ctx.hub_dir, actor.email)
        with access.identity_scope(identity):
            answer = "".join([chunk async for chunk in run_events.as_text(ctx.runtime.stream_events(prompt))])
        await asyncio.to_thread(svc.agent_reply_finish, pending, (answer or "").strip(), True)
    except Exception as exc:  # noqa: BLE001 - the thread shows the failure
        log.warning("spaces: agent reply in %s failed", sid, exc_info=True)
        await asyncio.to_thread(svc.agent_reply_finish, pending, f"The agent couldn't answer ({type(exc).__name__}).", False)


_loops: set[str] = set()


def _start_reminders(svc: SpacesService) -> None:
    """One reminder loop per hub in this process: due reminders go to the inbox."""
    key = str(svc.hub_dir.resolve())
    if key in _loops:
        return
    _loops.add(key)

    def loop():
        while svc.hub_dir.exists():
            try:
                svc.deliver_reminders()
            except Exception:  # noqa: BLE001 - try again next tick
                log.warning("spaces: reminder delivery failed", exc_info=True)
            time.sleep(REMINDER_SECONDS)

    threading.Thread(target=loop, daemon=True, name=f"hubzoid-reminders-{svc.hub}").start()
