"""Chat routes of the Hubzoid web app (contract 6.2).

  GET    /api/conversations                     the person's conversations (search, pages)
  POST   /api/conversations                     start one with this bridge's agent
  GET    /api/conversations/{id}                the conversation and its message tree
  PATCH  /api/conversations/{id}                title, archived, head_id
  DELETE /api/conversations/{id}                with its messages, shares and files
  POST   /api/chat                              send (or regenerate) and stream the reply
  POST   /api/runs/{message_id}/cancel          stop a reply
  GET    /api/runs/{message_id}                 a reply's status and content
  files and shares                              see ``files`` and ``shares``

Every route needs a signed-in person (``hubzoid.auth.require_user``); every
mutation must come from our own origin. Conversations are private to their
owner: anyone else gets 404. Calls that touch a hub's files or runtime (create,
delete, chat, runs, files) must reach that hub's bridge (else 409
``wrong_hub``) and need the person's ``use_hub`` access where they start work.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import memory as memlib
from . import files as files_mod
from . import shares as shares_mod
from . import store as store_mod
from . import titles
from .common import ChatContext, api_bases, db, error, message_json, not_found
from .history import build_prompt
from .runs import RunManager
from .store import remove_chat_files as _remove_chat_dir
from .store import (IdConflict, StoreError, chat_key, new_id, valid_conversation_id,
                    valid_message_id)
from .stream import HEADERS

log = logging.getLogger("hubzoid.chat")

MAX_MESSAGE_CHARS = 1_000_000
MAX_TITLE_CHARS = 200


def _max_files() -> int:
    try:
        return max(0, int(os.environ.get("HUBZOID_MAX_FILES_PER_MESSAGE", "10")))
    except ValueError:
        return 10


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise error(400, "invalid_json", "The request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise error(400, "invalid_json", "The request body must be a JSON object.")
    return body


def _store_error(exc: StoreError):
    return error(409 if isinstance(exc, IdConflict) else 400, exc.code, exc.message)


def _message_parts(ctx: ChatContext, key: str, message: dict, conversation_id: str | None = None) -> list[dict]:
    """Validate a new user message's parts; files come from the server's own
    upload records in the conversation's folder (its ``store.chat_key``).
    Raises HTTPException."""
    raw = message.get("content")
    if isinstance(raw, str):
        raw = [{"type": "text", "text": raw}]
    if not isinstance(raw, list):
        raise error(400, "invalid_message", "The message content must be a list of parts.")
    parts: list[dict] = []
    seen_files: set[str] = set()
    chars = 0
    for part in raw:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            text = part.get("text")
            if not isinstance(text, str):
                raise error(400, "invalid_message", "A text part needs a text string.")
            chars += len(text)
            if text:
                parts.append({"type": "text", "text": text})
        elif kind in ("file", "image"):
            file_id = part.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise error(400, "invalid_message", "A file part needs its file_id from the upload.")
            if file_id in seen_files:
                continue
            seen_files.add(file_id)
            if len(seen_files) > _max_files():
                raise error(400, "too_many_files",
                            f"Attach at most {_max_files()} files to one message.")
            stored = files_mod.file_part(ctx.hub_dir, key, file_id)
            if stored is None:
                raise error(400, "file_not_found",
                            f"The attached file {file_id!r} was not found. Upload it again.")
            svc = getattr(ctx, "files", None)
            blob = svc.file_for_local(conversation_id, file_id) if svc is not None and conversation_id else None
            if blob:
                stored["id"] = blob
            parts.append(stored)
        else:
            raise error(400, "invalid_message", "Messages hold text and file parts only.")
    if chars > MAX_MESSAGE_CHARS:
        raise error(413, "message_too_long", "That message is too long. Attach it as a file instead.")
    if not parts:
        raise error(400, "empty_message", "Write a message or attach a file.")
    return parts


def _search_text(parts: list[dict]) -> str:
    """The plain text stored for search: the words, then attachment names."""
    words = [p["text"] for p in parts if p.get("type") == "text"]
    names = [p.get("name") or p.get("file_id") for p in parts if p.get("type") in ("file", "image")]
    return "\n\n".join(words + ([" ".join(names)] if names else []))


def _first_words(parts: list[dict]) -> str:
    text = " ".join(p["text"] for p in parts if p.get("type") == "text").strip()
    if text:
        return text
    return ", ".join(p.get("name") or p.get("file_id") for p in parts
                     if p.get("type") in ("file", "image"))


def mount(app: FastAPI, hub_dir: Path, *, runtime=None, inflight=None, settings=None,
          model_label: str | None = None, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    if runtime is None or settings is None:
        log.warning("chat: not mounted (no runtime or settings)")
        return
    ctx = ChatContext(hub_dir=hub_dir, runtime=runtime, inflight=inflight, settings=settings,
                      model_label=model_label or getattr(runtime, "name", "agent"),
                      store=store_mod.for_hub(hub_dir))
    ctx.runs = RunManager(ctx)
    from . import engine_routes
    from .engine import ChatEngine

    def validate_parts(conv: dict, content) -> list[dict]:
        try:
            return _message_parts(ctx, chat_key(conv), {"content": content}, conv["id"])
        except HTTPException as exc:
            from .graph import GraphError

            raise GraphError(exc.status_code, exc.detail["code"], exc.detail["message"])

    def groups_for(email: str) -> frozenset[str]:
        from .. import access

        try:
            return frozenset(access.effective_groups(hub_dir, email=email, surface="web", header_groups=None))
        except Exception:  # noqa: BLE001 - no groups is the fail-closed default
            return frozenset()

    ctx.engine = ChatEngine(hub_dir, ctx.store, ctx.hub, part_validator=validate_parts, groups_for=groups_for)
    # The engine's event bus first: the feature routes below chain onto its
    # notify hook (search reindexing) and its run-end hook (notices).
    engine_routes.mount(app, ctx)
    from . import (authoring_routes, feedback_routes, file_routes, models_routes, organization_routes,
                   retrieval_routes, sharing_routes)

    feedback_routes.mount(app, ctx)
    file_routes.mount(app, ctx)
    retrieval_routes.mount(app, ctx)
    models_routes.mount(app, ctx)
    authoring_routes.mount(app, ctx)
    sharing_routes.mount(app, ctx)
    organization_routes.mount(app, ctx)
    from ..hitl import routes as hitl_routes

    hitl_routes.mount(app, ctx)
    from ..workflows import access_routes as workflow_access_routes

    workflow_access_routes.mount(app, ctx)
    from ..hub_changes import routes as hub_change_routes

    hub_change_routes.mount(app, ctx)
    try:
        ctx.engine.sweep()
    except Exception:  # noqa: BLE001 - never block the bridge start
        log.warning("chat: could not check for interrupted runs", exc_info=True)
    # After the run sweep: a reply whose run kept a live lease (another worker) stays.
    ctx.runs.sweep()
    app.state.chat = ctx
    store = ctx.store
    engine = ctx.engine
    call = engine_routes.call

    # -- conversations -----------------------------------------------------------
    @app.get("/api/conversations")
    async def list_conversations(request: Request):
        user = await ctx.user(request)
        params = request.query_params
        archived = (params.get("archived") or "0").strip().lower() in ("1", "true", "yes")
        raw_limit = params.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else store_mod.DEFAULT_PAGE
        except ValueError:
            raise error(400, "invalid_limit", "limit must be a number.")
        try:
            items, next_cursor = await db(store.list_conversations, user.id, archived=archived,
                                          q=params.get("q"), limit=limit,
                                          cursor=params.get("cursor") or None)
        except StoreError as exc:
            raise _store_error(exc)
        bases = api_bases(hub_dir)
        return {"items": [ctx.conversation_json(c, bases) for c in items],
                "next_cursor": next_cursor}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation(request: Request):
        user = await ctx.mutation(request)
        body = await engine_routes.read_json(request)
        agent = body.get("agent") or body.get("agent_id")
        if not agent:
            raise error(400, "agent_required", "Choose an agent to start a conversation.")
        if agent != ctx.model_label:
            raise error(404, "agent_not_found", "That agent is not served here.")
        conv_id = body.get("id") or new_id("c_")
        if not valid_conversation_id(conv_id):
            raise error(400, "invalid_id", "Conversation ids are 8 to 64 letters, digits, '-' "
                                           "or '_', starting and ending with a letter or digit.")
        temporary = body.get("temporary", False)
        if not isinstance(temporary, bool):
            raise error(400, "invalid_request", "temporary is true or false.")
        await ctx.may_use_hub(user)
        existing = await db(store.get_conversation, conv_id)
        if existing is not None:
            if existing["owner_id"] == user.id and existing["hub"] == ctx.hub and not existing.get("deleted_at"):
                return JSONResponse({"conversation": ctx.conversation_json(existing)})
            raise error(409, "id_conflict", "That conversation id is already in use.")
        actor = await db(engine.actor, user)
        made = await call(engine.create_conversation, actor, agent=agent, temporary=temporary,
                          conversation_id=conv_id, key=request.headers.get("idempotency-key"))
        conv = await db(store.get_conversation, made["conversation_id"])
        return JSONResponse(status_code=201, content={"conversation": {**ctx.conversation_json(conv),
                                                                       "temporary": temporary,
                                                                       "expires_at": made["expires_at"]},
                                                      "branch": made["branch"]})

    @app.get("/api/conversations/{conv_id}")
    async def get_conversation(conv_id: str, request: Request):
        user = await ctx.user(request)
        actor = await db(engine.actor, user)

        def live(message_id: str):
            run = ctx.runs.active(message_id)
            return run.builder.snapshot() if run is not None else None

        snap = await call(engine.snapshot, conv_id, actor, live=live)
        snap["conversation"] = {**ctx.conversation_json(await db(store.get_conversation, conv_id)),
                                **snap["conversation"]}
        snap["head_id"] = (snap.get("view") or {}).get("view_head_id")
        return snap

    @app.patch("/api/conversations/{conv_id}")
    async def update_conversation(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        body = await engine_routes.read_json(request)
        updates: dict = {}
        if "title" in body:
            title = body["title"]
            if not isinstance(title, str) or not " ".join(title.split()):
                raise error(400, "invalid_title", "A title needs some text.")
            title = " ".join(title.split())
            if len(title) > MAX_TITLE_CHARS:
                raise error(400, "invalid_title", f"Keep titles under {MAX_TITLE_CHARS} characters.")
            updates.update(title=title, title_source="user")
        if "archived" in body:
            if not isinstance(body["archived"], bool):
                raise error(400, "invalid_archived", "archived is true or false.")
            updates["archived"] = body["archived"]
        if "head_id" in body:
            head = body["head_id"]
            if head is not None:
                target = await db(store.get_message, head) if valid_message_id(head) else None
                if target is None or target["conversation_id"] != conv_id:
                    raise error(400, "invalid_head", "head_id must be a message of this conversation.")
            updates["head_id"] = head
            actor = await db(engine.actor, user)
            await call(engine.set_view, conv_id, actor, branch_id=None, head_id=head, expected_revision=None)
        if updates:
            conv = await db(store.update_conversation, conv_id, **updates)
        return {"conversation": ctx.conversation_json(conv)}

    @app.delete("/api/conversations/{conv_id}", status_code=204)
    async def delete_conversation(conv_id: str, request: Request):
        """Tombstone the conversation (its runs must be stopped first)."""
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        ctx.this_hub(conv)
        body = await engine_routes.read_json(request)
        actor = await db(engine.actor, user)
        await call(engine.delete_conversation, conv_id, actor, expected_revision=body.get("expected_revision"),
                   key=request.headers.get("idempotency-key"))
        await db(_remove_chat_dir, hub_dir, chat_key(conv))
        return Response(status_code=204)

    # -- chat --------------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(request: Request):
        user = await ctx.mutation(request)
        body = await engine_routes.read_json(request, engine_routes.MESSAGE_BODY_LIMIT)
        conv_id = body.get("conversation_id")
        if not valid_conversation_id(conv_id):
            raise error(400, "invalid_id", "conversation_id is missing or not a valid id.")
        parent_id = body.get("parent_id")
        if parent_id is not None and not valid_message_id(parent_id):
            raise error(400, "invalid_id", "parent_id is not a valid message id.")
        message = body.get("message")
        if message is not None:
            if not isinstance(message, dict) or not valid_message_id(message.get("id")):
                raise error(400, "invalid_id", "message.id is missing or not a valid id.")
        elif not parent_id:
            raise error(400, "invalid_parent", "Regenerating needs the parent user message.")
        assistant_id = body.get("assistant_message_id") or new_id("m_")
        if not valid_message_id(assistant_id):
            raise error(400, "invalid_id", "assistant_message_id is not a valid id.")
        if message is not None and message["id"] == assistant_id:
            raise error(400, "invalid_id", "The reply needs its own id.")

        await ctx.may_use_hub(user)
        await _require_terms(hub_dir, user)
        conv = await db(store.get_conversation, conv_id)
        if conv is None:
            agent = body.get("agent")
            if not agent:
                raise error(400, "agent_required", "Choose an agent to start a conversation.")
            if agent != ctx.model_label:
                raise error(404, "agent_not_found", "That agent is not served here.")
            if message is None or parent_id is not None:
                raise error(400, "invalid_parent", "A new conversation starts with a message.")
            # A message that will be refused must not leave an empty conversation.
            await db(_message_parts, ctx, chat_key({"id": conv_id, "source": "web"}), message)
            for mid in (message["id"], assistant_id):
                if await db(store.get_message, mid) is not None:
                    raise error(409, "id_conflict", "That message id is already in use.")
            try:
                conv = await db(store.create_conversation, conv_id=conv_id, owner_id=user.id,
                                owner_email=user.email, hub=ctx.hub, agent=agent)
            except StoreError as exc:
                raise _store_error(exc)
        elif conv["owner_id"] != user.id:
            raise not_found()
        else:
            ctx.this_hub(conv)

        actor = await db(engine.actor, user)
        run = await _legacy_turn(ctx, conv, actor, body, parent_id, message, assistant_id)
        queue = ctx.runs.subscribe(run)
        return StreamingResponse(ctx.runs.sse(run, queue), media_type="text/event-stream",
                                 headers=HEADERS)

    # -- runs --------------------------------------------------------------------
    async def owned_message(user, message_id: str) -> dict:
        message = await db(store.get_message, message_id) if valid_message_id(message_id) else None
        if message is None:
            raise not_found("reply")
        conv = await db(store.owned_conversation, message["conversation_id"], user.id)
        if conv is None:
            raise not_found("reply")
        ctx.this_hub(conv)
        return message

    @app.post("/api/runs/{run_ref}/cancel", status_code=202)
    async def cancel_run(run_ref: str, request: Request):
        """Stop a reply: the branch's queue is parked first, partial text is kept.
        ``run_ref`` is a run id or (older clients) the reply's message id."""
        user = await ctx.mutation(request)
        body = await engine_routes.read_json(request)
        actor = await db(engine.actor, user)
        run_id = await db(engine.run_for_message, run_ref) if valid_message_id(run_ref) else None
        legacy = run_id is not None  # addressed by the reply's message id (the 1.x contract)
        run_id = run_id or run_ref
        out = await call(engine.request_cancel, run_id, actor, park_queue=body.get("park_queue", True) is not False)
        if out.get("already_terminal"):
            if legacy:
                status = {"stopped": "cancelled", "failed": "error", "interrupted": "error"}.get(out["status"],
                                                                                              out["status"])
                return JSONResponse(status_code=202, content={"status": status, "run_id": run_id})
            return JSONResponse(status_code=200, content={"status": out["status"], "run_id": run_id})
        stopping = await ctx.runs.cancel(out["message_id"])
        return JSONResponse(status_code=202, content={
            "status": "cancelling" if stopping else out["status"], "run_id": run_id})

    @app.get("/api/runs/{message_id}")
    async def run_status(message_id: str, request: Request):
        user = await ctx.user(request)
        actor = await db(engine.actor, user)
        run_id = await db(engine.run_for_message, message_id) if valid_message_id(message_id) else message_id
        if run_id is None:
            message = await owned_message(user, message_id)
            out = message_json(message)
            return {"status": out["status"], "message": out}
        info = await call(engine.run, run_id, actor)
        out = info["message"] or {}
        live = ctx.runs.active(info["message_id"])
        if live is not None:
            out["content"] = live.builder.snapshot()
            out["status"] = "running"
        return {"status": out.get("status"), "message": out, "run": {k: v for k, v in info.items() if k != "message"}}

    files_mod.register(app, ctx)
    shares_mod.register(app, ctx)
    _stop_replies_on_shutdown(app, ctx.runs)
    log.info("chat: mounted for hub %s (agent %s)", ctx.hub, ctx.model_label)


def terms_required(hub_dir: Path, user_id: str) -> bool:
    """Whether this person must accept the current terms before starting work.
    Raises 503 when that can't be confirmed (fail closed)."""
    from ..ui import branding_store

    try:
        return bool(branding_store.service_for(hub_dir).me(user_id).get("terms_required"))
    except Exception:  # noqa: BLE001 - fail closed: required terms can't be confirmed
        log.warning("chat: terms state unavailable", exc_info=True)
        raise error(503, "unavailable", "Couldn't check the terms of use just now. Try again shortly.")


async def _require_terms(hub_dir: Path, user) -> None:
    """The deployment's terms, when its brand requires acceptance, are accepted
    by this person for the current version before they start work."""
    from ..ui import branding_store

    try:
        me = await asyncio.to_thread(branding_store.service_for(hub_dir).me, user.id)
    except Exception:  # noqa: BLE001 - fail closed: required terms can't be confirmed
        log.warning("chat: terms state unavailable", exc_info=True)
        raise error(503, "unavailable", "Couldn't check the terms of use just now. Try again shortly.")
    if me.get("terms_required"):
        raise error(403, "terms_required",
                    "Accept the current terms of use first. Reload the page to see them.")


async def _legacy_turn(ctx: ChatContext, conv: dict, actor, body: dict, parent_id: str | None,
                       message: dict | None, assistant_id: str):
    """``POST /api/chat`` (the 1.x streaming contract) as an engine operation:
    a new message extends the writer's branch whose head is ``parent_id`` (one
    is opened from there when needed); a regeneration is ``regenerate``."""
    from . import engine_routes
    from . import graph as g
    from .engine import Err

    engine = ctx.engine

    def plan_turn():
        import sqlalchemy as sa

        with engine.engine.begin() as conn:
            g.ensure_initial_branch(conn, conv, actor.id)
            rows = conn.execute(sa.select(g.branches).where(g.branches.c.conversation_id == conv["id"],
                                                            g.branches.c.writer_actor_id == actor.id)).all()
            match = next((b for b in rows if b.head_id == parent_id), None)
            if match is None:
                b = g.create_branch(conn, conv["id"], actor.id, parent_id)
                return b["id"], b["revision"]
            return match.id, match.revision

    existing = await db(ctx.store.get_message, message["id"]) if message is not None else None
    if existing is not None:
        # A retried send (same user message id): answer that saved message again.
        if (existing["conversation_id"] != conv["id"] or existing["role"] != "user"
                or existing.get("parent_id") != parent_id):
            raise error(409, "id_conflict", "That message id is already in use.")
        op = {"kind": "regenerate", "target_message_id": existing["id"], "assistant_message_id": assistant_id}
    elif message is not None:
        if parent_id is not None:
            parent = await db(ctx.store.get_message, parent_id)
            if parent is None or parent["conversation_id"] != conv["id"] or parent["role"] != "assistant":
                # The 1.x contract: a new message follows a reply.
                raise error(400, "invalid_parent", "A new message follows a reply; a regenerated reply "
                                                   "follows a user message.")
        branch_id, revision = await engine_routes.call(plan_turn)
        op = {"kind": "send", "branch_id": branch_id, "expected_revision": revision, "parent_id": parent_id,
              "content": message.get("content"), "message_id": message["id"], "assistant_message_id": assistant_id}
    else:
        op = {"kind": "regenerate", "target_message_id": parent_id, "assistant_message_id": assistant_id}
    try:
        out, plan = await asyncio.to_thread(engine.submit, conv["id"], actor, op, key=None,
                                            route="POST /api/chat")
    except Err as exc:
        if exc.code in ("branch_busy", "concurrency_limit"):
            raise error(409, "run_in_progress", "A reply is still being written. Stop it or wait for it to finish.")
        if exc.code in ("branch_conflict", "invalid_operation"):
            raise error(400, "invalid_parent", exc.message)
        if exc.code == "not_found":
            raise error(400, "invalid_parent", "parent_id is not a message of this conversation.")
        raise engine_routes.http(exc)
    run = await ctx.orchestrator.launch(plan)
    if run is None:
        raise error(503, "unavailable", "The reply could not start. Try again.")
    return run


def _stop_replies_on_shutdown(app: FastAPI, runs: RunManager) -> None:
    """Stop and save in-progress replies before the bridge's own shutdown
    (which closes the runtime they use)."""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with original(app_) as state:
            orch = getattr(runs.ctx, "orchestrator", None)
            if orch is not None:
                # Recover replies left by a worker that is gone once their
                # leases expire (the start-up sweep left unexpired ones alone).
                orch._ensure_heartbeat()
            try:
                yield state
            finally:
                try:
                    await runs.shutdown()
                except Exception:  # noqa: BLE001 — shutdown continues regardless
                    log.warning("chat: stopping replies on shutdown failed", exc_info=True)

    app.router.lifespan_context = lifespan
