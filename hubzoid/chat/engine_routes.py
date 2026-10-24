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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import ClientDisconnect

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
#: How often a worker looks for stops recorded elsewhere (another worker, the CLI).
CANCEL_POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 15.0
_WAKE = object()


async def read_json(request: Request, limit: int = SMALL_BODY_LIMIT) -> dict:
    """The JSON object body, counting bytes as they arrive."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise error(413, "request_too_large", "That request is too large.")
    chunks, size = [], 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise error(413, "request_too_large", "That request is too large.")
            chunks.append(chunk)
    except ClientDisconnect:
        # The browser left (a page change, a closed tab): nobody reads the
        # answer, so end quietly rather than log a server error.
        raise error(400, "client_disconnected", "The request ended before its body arrived.") from None
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

    async def launch(self, plan: RunPlan | list | None):
        if plan is None:
            return None
        if isinstance(plan, list):  # a comparison: one reply per model, at once
            runs = [await self.launch(p) for p in plan]
            return runs[0] if runs else None
        from . import titles
        from .history import build_prompt

        if plan.kind == "send":
            # Writing instead of answering (sent now or from the queue): the
            # waiting requests on this branch are cancelled, so the new
            # message steers the agent.
            from ..hitl import service as hitl_service

            try:
                await db(hitl_service.cancel_waiting_in_branch, self.ctx.hub_dir, self.ctx.hub, plan.branch_id,
                         reason="The person wrote a new message instead.")
            except Exception:  # noqa: BLE001 - never blocks the reply
                log.warning("chat: could not cancel waiting requests on %s", plan.branch_id, exc_info=True)
        try:
            conv = await db(self.ctx.store.get_conversation, plan.conversation_id)
            path = await db(self.engine.path, plan.conversation_id, plan.prompt_message_id)
            key = chat_key(conv)
            turns_prompt = await db(build_prompt, self.ctx.hub_dir, key, path)
            from .. import personal_memory

            notes = await db(personal_memory.prompt_block, self.ctx.hub_dir, plan.actor_id)
            if notes:  # the person's own notes, read at the start of the native session
                turns_prompt = f"{notes}\n\n{turns_prompt}"
            if plan.extra_turns:
                from .. import server

                turns_prompt = "\n\n".join(x for x in (turns_prompt, server._flatten_messages(plan.extra_turns)) if x)
            # The credential that started the reply goes with it: B's per-call check
            # stops the run when that session or key is revoked.
            user = SimpleNamespace(id=plan.actor_id, email=plan.actor_email,
                                   credential=plan.credential, credential_id=plan.credential_id,
                                   hubs=plan.key_hubs)
            from .. import _request_ctx
            from .history import user_text

            resume = await db(self.engine.binding_for, plan, self.config_digest(plan.actor_id))
            slot = _request_ctx.SessionSlot(
                resume_id=resume, delta_prompt=user_text(self.ctx.hub_dir, key, path[-1].get("content"))
                if resume and path else None)
            # Tool admission: no new step once the run was stopped or lost its lease.
            slot.guard = lambda: self.engine.run_state(plan.run_id, plan.branch_id, plan.fence) == "ok"
            opts = plan.options or {}
            if opts.get("web_search"):
                slot.web_search = await db(self._web_allowed, plan.actor_email)
            if opts.get("model"):
                models = getattr(self.ctx, "models", None)
                if models is not None and await db(models.allowed, opts["model"], plan.actor_email):
                    slot.model = opts["model"]
            if opts.get("tool_allowlist") is not None:
                slot.tool_allowlist = list(opts["tool_allowlist"])
            # Who this reply acts for, for approvals and questions (hubzoid.hitl).
            via_key = plan.credential == "api_key"
            slot.hitl = {"account_id": plan.actor_id, "email": plan.actor_email, "present": not via_key,
                         "origin": "api" if via_key else "chat",
                         "conversation_id": plan.conversation_id, "branch_id": plan.branch_id,
                         "message_id": plan.message_id, "run_id": plan.run_id}
            run = self.ctx.runs.start(conversation_id=plan.conversation_id, chat_key=key,
                                      message_id=plan.message_id, prompt=turns_prompt, user=user,
                                      title=conv.get("title") if plan.title_from else None,
                                      run_id=plan.run_id, branch_id=plan.branch_id, fence=plan.fence,
                                      prefix=plan.prefix, on_update=self._update, on_finish=self._finished,
                                      session_slot=slot,
                                      guard=lambda: self.engine.run_state(plan.run_id, plan.branch_id, plan.fence))
            run.builder.waiting_source = slot
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

    def _web_allowed(self, email: str) -> bool:
        """The runtime's own web search, this turn: the deployment allows it and
        the person holds the chat_web_search grant."""
        from . import features
        from .config import service_for

        try:
            web = service_for(self.ctx.hub_dir).active().document["retrieval"]["web"]
            return bool(web["enabled"] and web["search"] == "native"
                        and features.for_person(self.ctx.hub_dir, email).get("chat_web_search"))
        except Exception:  # noqa: BLE001 - unknown policy: no search
            return False

    def config_digest(self, actor_id: str = "") -> str:
        """What a native session depends on: the acting person, the agent, its
        runtime and model, its instructions and the chat policy revision. Any
        change starts a new native context from the graph."""
        from ..control import canonical
        from .config import service_for

        rt = self.ctx.runtime
        try:
            instructions = (Path(self.ctx.hub_dir) / "AGENTS.md").read_bytes()
        except OSError:
            instructions = b""
        try:
            policy = service_for(self.ctx.hub_dir).active().digest
        except Exception:  # noqa: BLE001 - unknown policy: never reuse a session
            policy = str(time.time())
        from .. import personal_memory

        return canonical.digest({"actor": actor_id, "agent": self.ctx.model_label, "runtime": type(rt).__name__,
                                 "model": str(getattr(rt, "name", "")),
                                 "instructions": canonical.digest_bytes(instructions), "policy": policy,
                                 "notes": personal_memory.revision(self.ctx.hub_dir, actor_id) if actor_id else 0})

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
        slot = run.session_slot
        try:
            if run.fenced_out:
                raise RuntimeError("the run lost its branch lease")
            await db(self.engine.record_binding, branch_id=run.branch_id, conversation_id=run.conversation_id,
                     runtime=getattr(slot, "runtime", None), session_id=getattr(slot, "session_id", None),
                     head_message_id=run.message_id, config_digest=self.config_digest(run.owner_id),
                     ok=status == "complete")
        except Exception:  # noqa: BLE001 - a lost binding only means a full prompt next time
            log.warning("chat: could not record the runtime session of %s", run.run_id, exc_info=True)
        files = getattr(self.ctx, "files", None)
        if files is not None and not run.fenced_out:
            try:  # documents the run wrote become artifact versions
                await db(files.capture_artifacts, conversation_id=run.conversation_id, branch_id=run.branch_id,
                         actor_id=run.owner_id, run_id=run.run_id, message_id=run.message_id,
                         since=time.time() - (time.monotonic() - run.started))
            except Exception:  # noqa: BLE001 - the reply stands; its files stay in the folder
                log.warning("chat: could not record the artifacts of %s", run.run_id, exc_info=True)
        try:
            # A writer that lost its branch lease was cut off, not stopped by a
            # person: its run ends interrupted, as the sweep would record it.
            nxt = await db(self.engine.run_finished, run.run_id,
                           "interrupted" if run.fenced_out else status, error_text)
        except Exception:  # noqa: BLE001
            log.exception("chat: could not record the end of run %s", run.run_id)
            return
        await self.launch(nxt)
        if status == "complete" and not run.fenced_out:
            await self.continue_settled(run.message_id)

    # -- approvals and questions ------------------------------------------------------
    async def continue_request(self, request: dict, *, app_caller=None, on_run=None) -> bool:
        """A person answered a request this chat made: continue the branch with
        their answer, once. Waits for the reply that asked to end first.

        A request a person in an embedding app made continues only with that
        person back, holding a fresh token (``app_caller``, from the AG-UI
        resume): never from here alone, since nothing else holds their token."""
        from ..hitl import service

        if request.get("origin") != "chat" or not request.get("conversation_id") \
                or request.get("status") not in ("approved", "rejected", "answered"):
            return False
        from ..embed.caller import is_app_subject

        if is_app_subject(request.get("requester_id")):
            if app_caller is None or app_caller.subject != request.get("requester_id"):
                return False
            from ..embed import scope as app_scope

            if not app_scope.hand_over(request["conversation_id"], app_caller):
                return False
        from .. import drain

        if drain.active():  # continued by the recovery loop after the restart
            self._release_app(request, app_caller)
            return False
        # The reply resumes for the person who asked, on the session or key
        # that reply ran with: if that one was revoked (or they were signed
        # out everywhere) since, it doesn't resume.
        credential = await db(_origin_credential, self.ctx.hub_dir, request["message_id"])
        from ..access.evaluator import still_valid

        if credential and not await db(lambda: still_valid(self.ctx.hub_dir, request["requester_id"],
                                                           credential, expired_ok=True)):
            log.info("chat: request %s not continued: its sign-in ended", request["id"])
            self._release_app(request, app_caller)
            return False
        if not await db(service.mark_continued, self.ctx.hub_dir, request["id"]):
            self._release_app(request, app_caller)
            return False
        parts = [{"type": "text", "text": service.visible_outcome(request) or "OK"},
                 {"type": "request_outcome", "request_id": request["id"],
                  "for_model": service.outcome_text(request)}]
        try:
            plan = await db(self.engine.continue_after_request, request["conversation_id"],
                            actor_id=request["requester_id"], branch_id=request["branch_id"],
                            after_message_id=request["message_id"], parts=parts, credential=credential)
        except self.engine.Busy:
            await db(_unclaim, self.ctx.hub_dir, request["id"])  # retried when that reply ends
            self._release_app(request, app_caller)
            return False
        except Exception:  # noqa: BLE001 - the decision stands; recovery tries again
            log.exception("chat: could not continue after request %s", request["id"])
            await db(_unclaim, self.ctx.hub_dir, request["id"])
            self._release_app(request, app_caller)
            return False
        run = await self.launch(plan)
        if run is None and app_caller is not None:
            self._release_app(request, app_caller)
        if on_run is not None and run is not None:
            on_run(run)
        return plan is not None

    @staticmethod
    def _release_app(request: dict, app_caller) -> None:
        if app_caller is not None:
            from ..embed import scope as app_scope

            app_scope.release(request["conversation_id"], app_caller)

    async def continue_settled(self, message_id: str) -> None:
        from ..hitl import service

        for req in await db(_settled_waiting, self.ctx.hub_dir, self.ctx.hub, message_id):
            await self.continue_request(req)

    def _ensure_heartbeat(self) -> None:
        if self._beat is None or self._beat.done():
            self._beat = asyncio.ensure_future(self._heartbeat())

    async def _heartbeat(self) -> None:
        """While this bridge writes replies: every second, stop the ones whose
        stop was recorded elsewhere; every ten, renew their branch leases (a run
        whose lease was lost stops and writes nothing more), end expired
        temporary conversations and recover runs of workers that are gone."""
        ticks = 0
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            ticks += 1
            runs = [r for r in self.ctx.runs.active_runs() if r.run_id]
            try:
                for rid in await db(self.engine.cancel_requested, [r.run_id for r in runs]):
                    run = next(r for r in runs if r.run_id == rid)
                    await self.ctx.runs.cancel(run.message_id)
            except Exception:  # noqa: BLE001
                log.warning("chat: stop check failed", exc_info=True)
            if ticks % int(HEARTBEAT_SECONDS / CANCEL_POLL_SECONDS):
                continue
            for r in runs:
                try:
                    ok = await db(g.renew_lease, self.engine.engine, r.branch_id, r.run_id, r.fence)
                except Exception:  # noqa: BLE001 - tried again next beat; an expired lease stops the run
                    log.warning("chat: could not renew the lease of run %s", r.run_id, exc_info=True)
                    continue
                if not ok:
                    log.warning("chat: run %s lost its branch lease; stopping it", r.run_id)
                    r.fenced_out = True
                    await self.ctx.runs.cancel(r.message_id)
            try:
                await db(self.engine.expire_temporary)
                if await db(self.engine.sweep):
                    await db(self.ctx.runs.sweep)
            except Exception:  # noqa: BLE001
                log.warning("chat: maintenance sweep failed", exc_info=True)
            try:
                if not runs and not await db(self.engine.has_active_runs):
                    return
            except Exception:  # noqa: BLE001 - keep beating until the store answers
                log.warning("chat: could not check for running replies", exc_info=True)


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

    def admission(actor: Actor) -> None:
        """May this person start a reply now: hub use and the current terms.
        Used before direct operations, queued messages, and again when queued
        work leaves the queue."""
        from fastapi import HTTPException

        from .common import check_use_hub
        from .routes import terms_required

        try:
            check_use_hub(ctx.hub_dir, actor.email)
            needs_terms = terms_required(ctx.hub_dir, actor.id)
        except HTTPException as exc:
            raise g.GraphError(exc.status_code, exc.detail["code"], exc.detail["message"])
        if needs_terms:
            raise g.GraphError(403, "terms_required", "Accept the current terms of use first. Reload the page "
                                                      "to see them.")

    engine.admission = admission

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
        if body.get("kind") in ("send", "respond", "revise", "regenerate", "continue", "synthesize", "compare"):
            await call(admission, actor)
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
        await call(admission, actor)
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
        from .common import check_use_hub

        actor = await actor_of(request)
        # Agent access is checked before anything is replayed, as it is again
        # before each later delivery.
        await asyncio.to_thread(check_use_hub, ctx.hub_dir, actor.email)
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
                    # Every delivery, persisted or live, goes only to someone who
                    # may read now: their session still valid and their current
                    # grants and groups (re-resolved, never the ones at connect).
                    try:
                        from ..auth import sessions as sessionlib
                        from .common import check_use_hub

                        # The answer cached on this request at connect time is
                        # dropped, so a signed-out device, revoked key, block
                        # or deletion is seen at the next delivery.
                        sessionlib.forget(request)
                        now_actor = await actor_of(request)
                        if now_actor.id != actor.id:
                            raise g.GraphError(403, "permission_denied", "The signed-in person changed.")
                        await asyncio.to_thread(check_use_hub, ctx.hub_dir, now_actor.email)
                        await asyncio.to_thread(engine.readable, cid, now_actor)
                    except HTTPException as exc:
                        # The account store briefly unavailable isn't a
                        # revocation: close, and the browser reconnects.
                        if exc.status_code < 500:
                            yield _sse({"type": "access.revoked"}, None)
                        return
                    except g.GraphError:
                        yield _sse({"type": "access.revoked"}, None)
                        return
                    if item is _WAKE:
                        batch = await asyncio.to_thread(g.events_after, engine.engine, cid, cursor)
                    else:
                        yield _sse(item, None)
            finally:
                bus.unsubscribe(cid, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return orch


def _origin_credential(hub_dir: Path, message_id: str) -> dict | None:
    """The session or key, and credential counter, the asking reply ran with
    (its run identity record), or None when there's no such record."""
    from ..access import run_identity

    for kind in ("chat", "api"):
        rec = run_identity.for_run(Path(hub_dir), kind, message_id)
        if rec is not None:
            scope = rec.get("scope") or {}
            return {"kind": rec.get("credential_kind") or "", "id": rec.get("credential_id") or "",
                    "hubs": scope.get("hubs") if isinstance(scope, dict) else None,
                    "epoch": rec.get("auth_epoch")}
    return None


def _unclaim(hub_dir: Path, request_id: str) -> None:
    import sqlalchemy as sa

    from ..hitl import service, store

    with service.engine(hub_dir).begin() as conn:
        conn.execute(store.requests.update().where(store.requests.c.id == request_id).values(continued=False))


def _settled_waiting(hub_dir: Path, hub: str, message_id: str) -> list[dict]:
    """Requests asked by this reply that were decided but not yet continued."""
    import sqlalchemy as sa

    from ..hitl import service, store

    r = store.requests.c
    with service.engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(store.requests).where(
            r.hub_id == hub, r.message_id == message_id, r.continued.is_(False),
            r.status.in_(("approved", "rejected", "answered")))).fetchall()
    return [store.row_dict(x) for x in rows]


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
