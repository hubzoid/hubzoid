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

from fastapi import FastAPI, Request
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


def _message_parts(ctx: ChatContext, key: str, message: dict) -> list[dict]:
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
    ctx.runs.sweep()
    app.state.chat = ctx
    store = ctx.store

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
        body = await _body(request)
        agent = body.get("agent")
        if not agent:
            raise error(400, "agent_required", "Choose an agent to start a conversation.")
        if agent != ctx.model_label:
            raise error(404, "agent_not_found", "That agent is not served here.")
        conv_id = body.get("id") or new_id("c_")
        if not valid_conversation_id(conv_id):
            raise error(400, "invalid_id", "Conversation ids are 8 to 64 letters, digits, '-' "
                                           "or '_', starting and ending with a letter or digit.")
        await ctx.may_use_hub(user)
        existing = await db(store.get_conversation, conv_id)
        if existing is not None:
            if existing["owner_id"] == user.id and existing["hub"] == ctx.hub:
                return JSONResponse({"conversation": ctx.conversation_json(existing)})
            raise error(409, "id_conflict", "That conversation id is already in use.")
        try:
            conv = await db(store.create_conversation, conv_id=conv_id, owner_id=user.id,
                            owner_email=user.email, hub=ctx.hub, agent=agent)
        except StoreError as exc:
            raise _store_error(exc)
        return JSONResponse(status_code=201, content={"conversation": ctx.conversation_json(conv)})

    @app.get("/api/conversations/{conv_id}")
    async def get_conversation(conv_id: str, request: Request):
        user = await ctx.user(request)
        conv = await ctx.owned(user, conv_id)
        messages = [message_json(m) for m in await db(store.list_messages, conv_id)]
        for message in messages:
            live = ctx.runs.active(message["id"])
            if live is not None:
                message["content"] = live.builder.snapshot()
        head = conv.get("head_id")
        if not head and messages:
            head = await db(store.latest_leaf, conv_id)
        return {"conversation": ctx.conversation_json(conv), "messages": messages, "head_id": head}

    @app.patch("/api/conversations/{conv_id}")
    async def update_conversation(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        body = await _body(request)
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
        if updates:
            conv = await db(store.update_conversation, conv_id, **updates)
        return {"conversation": ctx.conversation_json(conv)}

    @app.delete("/api/conversations/{conv_id}", status_code=204)
    async def delete_conversation(conv_id: str, request: Request):
        user = await ctx.mutation(request)
        conv = await ctx.owned(user, conv_id)
        ctx.this_hub(conv)
        live = ctx.runs.active_in(conv_id)
        if live is not None:
            await ctx.runs.cancel(live.message_id)
            try:
                await asyncio.wait_for(live.done.wait(), timeout=15)
            except asyncio.TimeoutError:
                log.warning("chat: reply %s did not stop before delete", live.message_id)
        await db(store.delete_conversation, conv_id)
        await db(_remove_chat_dir, hub_dir, chat_key(conv))
        return Response(status_code=204)

    # -- chat --------------------------------------------------------------------
    @app.post("/api/chat")
    async def chat(request: Request):
        user = await ctx.mutation(request)
        body = await _body(request)
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

        if not ctx.runs.claim(conv_id):
            raise error(409, "run_in_progress",
                        "A reply is still being written. Stop it or wait for it to finish.")
        try:
            prepared = await _prepare_turn(ctx, conv, body, parent_id, message, assistant_id)
            run = ctx.runs.start(conversation_id=conv_id, chat_key=chat_key(conv),
                                 message_id=assistant_id, prompt=prepared["prompt"], user=user,
                                 title=prepared["title"])
        except BaseException:
            ctx.runs.release(conv_id)
            raise
        if prepared["title_from"] is not None:
            titles.start(store, hub_dir, conv_id, prepared["title_from"], user.email,
                         on_title=lambda title: ctx.runs.title_ready(conv_id, title))
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

    @app.post("/api/runs/{message_id}/cancel", status_code=202)
    async def cancel_run(message_id: str, request: Request):
        user = await ctx.mutation(request)
        message = await owned_message(user, message_id)
        stopping = await ctx.runs.cancel(message_id)
        return JSONResponse(status_code=202, content={
            "status": "cancelling" if stopping else message.get("status")})

    @app.get("/api/runs/{message_id}")
    async def run_status(message_id: str, request: Request):
        user = await ctx.user(request)
        message = await owned_message(user, message_id)
        out = message_json(message)
        live = ctx.runs.active(message_id)
        if live is not None:
            out["content"] = live.builder.snapshot()
            out["status"] = "running"
        return {"status": out["status"], "message": out}

    files_mod.register(app, ctx)
    shares_mod.register(app, ctx)
    _stop_replies_on_shutdown(app, ctx.runs)
    log.info("chat: mounted for hub %s (agent %s)", ctx.hub, ctx.model_label)


async def _prepare_turn(ctx: ChatContext, conv: dict, body: dict, parent_id: str | None,
                        message: dict | None, assistant_id: str) -> dict:
    """Store the user message (idempotent by id) and the 'running' reply, and
    build the prompt from the branch. Returns the prompt, the provisional title
    when this message set it, and the text to title from when a title is due."""
    store = ctx.store
    conv_id = conv["id"]
    key = chat_key(conv)
    if await db(store.get_message, assistant_id) is not None:
        raise error(409, "id_conflict", "That reply id is already in use.")
    if parent_id is not None:
        parent = await db(store.get_message, parent_id)
        if parent is None or parent["conversation_id"] != conv_id:
            raise error(400, "invalid_parent", "parent_id is not a message of this conversation.")
        wanted = "assistant" if message is not None else "user"
        if parent["role"] != wanted:
            raise error(400, "invalid_parent",
                        "A new message follows a reply; a regenerated reply follows a user message.")

    title = None
    title_from = None
    if message is not None:
        existing = await db(store.get_message, message["id"])
        if existing is not None:
            if (existing["conversation_id"] != conv_id or existing["role"] != "user"
                    or existing.get("parent_id") != parent_id):
                raise error(409, "id_conflict", "That message id is already in use.")
            user_id = existing["id"]
        else:
            parts = await db(_message_parts, ctx, key, message)
            text = _search_text(parts)
            try:
                await db(store.insert_message, message_id=message["id"], conversation_id=conv_id,
                         parent_id=parent_id, role="user", content=parts, text=text)
            except StoreError as exc:
                raise _store_error(exc)
            user_id = message["id"]
            if conv.get("title_source") == "pending" and not conv.get("title"):
                subject = _first_words(parts)
                title = titles.provisional(subject)
                await db(store.set_title, conv_id, title, "pending", only_if_source=("pending",))
                title_from = subject
    else:
        user_id = parent_id

    try:
        await db(store.insert_message, message_id=assistant_id, conversation_id=conv_id,
                 parent_id=user_id, role="assistant", content=[], text="", status="running")
    except StoreError as exc:
        raise _store_error(exc)
    await db(store.touch, conv_id, head_id=assistant_id)
    branch = await db(store.branch, conv_id, user_id)
    prompt = await db(build_prompt, ctx.hub_dir, key, branch)
    return {"prompt": prompt, "title": title, "title_from": title_from}


def _stop_replies_on_shutdown(app: FastAPI, runs: RunManager) -> None:
    """Stop and save in-progress replies before the bridge's own shutdown
    (which closes the runtime they use)."""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_):
        async with original(app_) as state:
            try:
                yield state
            finally:
                try:
                    await runs.shutdown()
                except Exception:  # noqa: BLE001 — shutdown continues regardless
                    log.warning("chat: stopping replies on shutdown failed", exc_info=True)

    app.router.lifespan_context = lifespan
