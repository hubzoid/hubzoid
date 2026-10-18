"""HTTP routes of the chat operation engine, and the loop-side orchestrator.

  GET    /api/conversations/{id}/branches        branch index
  POST   /api/conversations/{id}/branches        {from_message_id}: a branch of your own
  PUT    /api/conversations/{id}/view            {branch_id, view_head_id, expected_revision}
  POST   /api/conversations/{id}/operations      send, respond, revise, regenerate, continue,
                                                 delete, fork, synthesize (Idempotency-Key)
  GET    /api/chat/operations/{id}               an operation's receipt
  GET    /api/conversations/{id}/events          SSE: persisted events (``id:`` = sequence)
                                                 and live reply snapshots
  POST   /api/conversations/{id}/queue           queue a follow-up (sent at once when idle)
  GET    /api/conversations/{id}/queue           queued messages
  PATCH  /api/conversations/{id}/queue/{item}    edit, move, remove, rebase
  POST   /api/conversations/{id}/queue-control   pause, resume, clear
  POST   /api/conversations/{id}/deletion-preview
  POST   /api/conversations/{id}/save            keep a temporary conversation
  GET    /api/chat/runs/{id}                     a run (``/api/runs/{id}`` accepts run ids too)

Chat JSON bodies are read with a byte cap (``read_json``): 413
``request_too_large`` before parsing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import graph as g
from .common import db, error
from .engine import Actor, ChatEngine, RunPlan
from .store import chat_key

log = logging.getLogger("hubzoid.chat.engine")

#: A message route may carry about 1,000,000 characters (escaped JSON can grow).
MESSAGE_BODY_LIMIT = 8 * 1024 * 1024
SMALL_BODY_LIMIT = 256 * 1024
LIVE_INTERVAL = 0.1
HEARTBEAT_SECONDS = 10.0
KEEPALIVE_SECONDS = 15.0
_WAKE = object()


async def read_json(request: Request, limit: int = SMALL_BODY_LIMIT) -> dict:
    """The JSON object body, counting bytes as they arrive."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise error(413, "request_too_large", "That request is too large.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise error(413, "request_too_large", "That request is too large.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise error(400, "invalid_json", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise error(400, "invalid_json", "The request body must be a JSON object.")
    return body


def http(exc: g.GraphError):
    from fastapi import HTTPException

    headers = {"Retry-After": str(exc.extra["retry_after"])} if "retry_after" in exc.extra else None
    return HTTPException(exc.status, detail={"code": exc.code, "message": exc.message, **exc.extra},
                         headers=headers)


async def call(fn, *args, **kwargs):
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except g.GraphError as exc:
        raise http(exc)


class EventBus:
    """Wakes event streams of a conversation (thread-safe) and carries live
    reply snapshots, which are not stored as events."""

    def __init__(self):
        self._subs: dict[str, set] = {}

    def subscribe(self, conversation_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.setdefault(conversation_id, set()).add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, conversation_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(conversation_id)
        if subs is None:
            return
        for entry in list(subs):
            if entry[1] is q:
                subs.discard(entry)
        if not subs:
            self._subs.pop(conversation_id, None)

    def _put(self, conversation_id: str, item) -> None:
        for loop, q in list(self._subs.get(conversation_id, ())):
            def put(q=q):
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    pass
            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                pass

    def wake(self, conversation_id: str) -> None:
        self._put(conversation_id, _WAKE)

    def live(self, conversation_id: str, payload: dict) -> None:
        self._put(conversation_id, payload)


class Orchestrator:
    """Launches admitted replies on the event loop, keeps their leases alive,
    relays live snapshots and admits the next queued message when one ends."""

    def __init__(self, ctx, engine: ChatEngine, bus: EventBus):
        self.ctx = ctx
        self.engine = engine
        self.bus = bus
        self._last_live: dict[str, float] = {}
        self._seq: dict[str, int] = {}
        self._beat: asyncio.Task | None = None

    async def launch(self, plan: RunPlan | None):
        if plan is None:
            return None
        from . import titles
        from .history import build_prompt

        try:
            conv = await db(self.ctx.store.get_conversation, plan.conversation_id)
            path = await db(self.engine.path, plan.conversation_id, plan.prompt_message_id)
            key = chat_key(conv)
            turns_prompt = await db(build_prompt, self.ctx.hub_dir, key, path)
            if plan.extra_turns:
                from .. import server

                turns_prompt = "\n\n".join(x for x in (turns_prompt, server._flatten_messages(plan.extra_turns)) if x)
            user = SimpleNamespace(id=plan.actor_id, email=plan.actor_email)
            run = self.ctx.runs.start(conversation_id=plan.conversation_id, chat_key=key,
                                      message_id=plan.message_id, prompt=turns_prompt, user=user,
                                      title=conv.get("title") if plan.title_from else None,
                                      run_id=plan.run_id, branch_id=plan.branch_id, fence=plan.fence,
                                      prefix=plan.prefix, on_update=self._update, on_finish=self._finished)
        except Exception as exc:  # noqa: BLE001 - the admission is released, never left running
            log.exception("chat: could not start run %s", plan.run_id)
            await db(self.ctx.store.update_message, plan.message_id, status="error",
                     error=f"The reply could not start: {type(exc).__name__}.")
            nxt = await db(self.engine.run_finished, plan.run_id, "error", str(exc))
            await self.launch(nxt)
            return None
        await db(self.engine.run_streaming, plan.run_id)
        if plan.title_from:
            titles.start(self.ctx.store, self.ctx.hub_dir, plan.conversation_id, plan.title_from, plan.actor_email,
                         on_title=lambda title: (self.ctx.runs.title_ready(plan.conversation_id, title),
                                                 self.bus.live(plan.conversation_id,
                                                               {"type": "conversation.titled", "title": title})))
        self._ensure_heartbeat()
        return run

    def _update(self, run) -> None:
        now = time.monotonic()
        if now - self._last_live.get(run.message_id, 0.0) < LIVE_INTERVAL:
            return
        self._last_live[run.message_id] = now
        seq = self._seq.get(run.message_id, 0) + 1
        self._seq[run.message_id] = seq
        self.bus.live(run.conversation_id, {"type": "message.updated", "object_id": run.message_id,
                                            "run_id": run.run_id, "branch_id": run.branch_id,
                                            "content_seq": seq, "content": run.builder.snapshot()})

    async def _finished(self, run, status: str, error_text: str | None) -> None:
        self._last_live.pop(run.message_id, None)
        self._seq.pop(run.message_id, None)
        if run.run_id is None:
            return
        try:
            nxt = await db(self.engine.run_finished, run.run_id, status, error_text)
        except Exception:  # noqa: BLE001
            log.exception("chat: could not record the end of run %s", run.run_id)
            return
        await self.launch(nxt)

    def _ensure_heartbeat(self) -> None:
        if self._beat is None or self._beat.done():
            self._beat = asyncio.ensure_future(self._heartbeat())

    async def _heartbeat(self) -> None:
        """Renew this bridge's branch leases; a run whose lease was lost stops."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            runs = [r for r in self.ctx.runs.active_runs() if r.run_id]
            for r in runs:
                ok = await db(g.renew_lease, self.engine.engine, r.branch_id, r.run_id, r.fence)
                if not ok:
                    log.warning("chat: run %s lost its branch lease; stopping it", r.run_id)
                    await self.ctx.runs.cancel(r.message_id)
            try:
                await db(self.engine.expire_temporary)
            except Exception:  # noqa: BLE001
                log.warning("chat: temporary-conversation sweep failed", exc_info=True)
            if not runs:
                return


def mount(app: FastAPI, ctx) -> Orchestrator:
    """Install the engine routes on the bridge (called by ``routes.mount``)."""
    engine: ChatEngine = ctx.engine
    bus = EventBus()
    engine.notify = bus.wake
    orch = Orchestrator(ctx, engine, bus)
    ctx.orchestrator = orch

    async def actor_of(request: Request, *, mutation: bool = False) -> Actor:
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return await asyncio.to_thread(engine.actor, user)

    def key_of(request: Request) -> str | None:
        return request.headers.get("idempotency-key")

    def live_content(message_id: str):
        run = ctx.runs.active(message_id)
        return run.builder.snapshot() if run is not None else None

    @app.get("/api/conversations/{cid}/branches")
    async def list_branches(cid: str, request: Request):
        actor = await actor_of(request)
        return {"items": await call(engine.branches, cid, actor)}

    @app.post("/api/conversations/{cid}/branches", status_code=201)
    async def create_branch(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request)
        return JSONResponse(await call(engine.create_branch, cid, actor, from_message_id=body.get("from_message_id"),
                                       key=key_of(request)), status_code=201)

    @app.put("/api/conversations/{cid}/view")
    async def put_view(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request)
        return await call(engine.set_view, cid, actor, branch_id=body.get("branch_id"),
                          head_id=body.get("view_head_id"), expected_revision=body.get("expected_revision"))

    @app.post("/api/conversations/{cid}/operations")
    async def operations(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request, MESSAGE_BODY_LIMIT)
        key = key_of(request)
        if not key:
            raise error(400, "invalid_request", "Send an Idempotency-Key header with every operation.")
        if body.get("kind") in ("send", "respond", "revise", "regenerate", "continue", "synthesize"):
            from .routes import _require_terms

            user = SimpleNamespace(id=actor.id, email=actor.email)
            await ctx.may_use_hub(user)
            await _require_terms(ctx.hub_dir, user)
        out, plan = await call(engine.submit, cid, actor, body, key=key)
        await orch.launch(plan)
        if body.get("kind") == "fork" and not out.replayed and out.status == 201:
            await db(_copy_files, ctx.hub_dir, out.body)
        return JSONResponse(out.body, status_code=out.status)

    @app.get("/api/chat/operations/{op_id}")
    async def operation(op_id: str, request: Request):
        actor = await actor_of(request)
        return await call(engine.operation, op_id, actor)

    @app.post("/api/conversations/{cid}/queue", status_code=201)
    async def enqueue(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request, MESSAGE_BODY_LIMIT)
        key = key_of(request)
        if not key:
            raise error(400, "invalid_request", "Send an Idempotency-Key header to queue a message.")
        out, plan = await call(engine.enqueue, cid, actor, body, key=key)
        await orch.launch(plan)
        return JSONResponse(out.body, status_code=out.status)

    @app.get("/api/conversations/{cid}/queue")
    async def queue_list(cid: str, request: Request):
        actor = await actor_of(request)
        return {"items": await call(engine.queue_list, cid, actor)}

    @app.patch("/api/conversations/{cid}/queue/{item_id}")
    async def queue_patch(cid: str, item_id: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request, MESSAGE_BODY_LIMIT)
        return await call(engine.queue_patch, cid, actor, item_id, body)

    @app.post("/api/conversations/{cid}/queue-control")
    async def queue_control(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request)
        branch, plan = await call(engine.queue_control, cid, actor, body)
        await orch.launch(plan)
        return {"branch": branch}

    @app.post("/api/conversations/{cid}/deletion-preview")
    async def deletion_preview(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request)
        return await call(engine.deletion_preview, cid, actor, message_id=body.get("message_id"),
                          expected_revision=body.get("expected_revision"))

    @app.post("/api/conversations/{cid}/save")
    async def save(cid: str, request: Request):
        actor = await actor_of(request, mutation=True)
        body = await read_json(request)
        return await call(engine.save_conversation, cid, actor, expected_revision=body.get("expected_revision"),
                          key=key_of(request))

    @app.get("/api/chat/runs/{run_id}")
    async def run(run_id: str, request: Request):
        actor = await actor_of(request)
        out = await call(engine.run, run_id, actor)
        content = live_content(out["message_id"])
        if content is not None and out.get("message"):
            out["message"]["content"] = content
        return out

    @app.get("/api/conversations/{cid}/events")
    async def events(cid: str, request: Request):
        actor = await actor_of(request)
        await call(engine.readable, cid, actor)
        raw = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
        try:
            after = int(raw)
        except ValueError:
            raise error(400, "invalid_request", "after is an event sequence number.")
        first = await call(g.events_after, engine.engine, cid, after)  # 410 before any byte is sent
        q = bus.subscribe(cid)

        async def stream():
            cursor = after
            try:
                batch = first
                yield b"retry: 2000\n\n"
                while True:
                    for ev in batch:
                        cursor = ev["sequence"]
                        yield _sse(ev, ev["sequence"])
                        if ev["type"] in ("conversation.deleted", "conversation.expired"):
                            return
                    batch = []
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        item = _WAKE
                    if await request.is_disconnected():
                        return
                    if item is _WAKE:
                        try:
                            await asyncio.to_thread(engine.readable, cid, actor)
                        except g.GraphError:
                            yield _sse({"type": "access.revoked"}, None)
                            return
                        batch = await asyncio.to_thread(g.events_after, engine.engine, cid, cursor)
                    else:
                        yield _sse(item, None)
            finally:
                bus.unsubscribe(cid, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return orch


def _sse(payload: dict, sequence: int | None) -> bytes:
    head = f"id: {sequence}\n" if sequence is not None else ""
    return (head + "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n").encode()


def _copy_files(hub_dir: Path, body: dict) -> None:
    """A fork's attachments: copy the source chat's uploads the copy refers to."""
    import shutil

    from .. import memory as memlib

    src = memlib.chat_upload_dir(Path(hub_dir), body["source_chat_key"])
    dst = memlib.chat_upload_dir(Path(hub_dir), body["new_chat_key"])
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and not f.is_symlink():
            shutil.copy2(f, dst / f.name)
