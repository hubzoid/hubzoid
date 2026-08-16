"""FastAPI bridge — exposes an OpenAI-compatible HTTP API in front of a Runtime.

Endpoints:
  GET  /healthz                          liveness probe
  GET  /v1/models                        [{ id, object, created, owned_by }]
  POST /v1/chat/completions              streaming SSE (OpenAI shape) + non-stream
  GET  /artifacts/{chat_id}/{filename}   download a file the agent wrote
  POST /uploads/{chat_id}/{filename}     upload a file the agent can read

The bridge is built around a single Runtime per process (one hub). The
Runtime is selected based on `MODEL` in <hub>/.env — see `hubzoid/runtime.py`.

Per-chat scoping. Each `/v1/chat/completions` request derives a chat id
(from `body.chat_id`, `body.metadata.chat_id`, the `X-Hubzoid-Chat-Id`
header, the OpenAI `body.user` field, or a hash fallback) and stores it
in a ContextVar so `write_artifact` and `read_upload` resolve their
directory per chat instead of per process. Any data-URL attachments in
the message body are persisted to the chat's uploads dir before the
prompt is flattened.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import _request_ctx
from . import _signing
from . import access
from . import memory as memlib
from . import owui as owui_lib
from . import runtime as runtime_lib
from . import settings as settingslib
from . import uploads as uploads_lib

log = logging.getLogger("hubzoid.server")


def _hub_dir() -> Path:
    p = os.environ.get("HUBZOID_HUB_DIR")
    if not p:
        raise RuntimeError("HUBZOID_HUB_DIR is not set. The bridge needs to know which hub to serve.")
    return Path(p).resolve()


def build_app() -> FastAPI:
    hub_dir = _hub_dir()
    settings = settingslib.load(hub_dir)
    rt = runtime_lib.build(hub_dir)

    # Model label shown in /v1/models. Falls back to the runtime's name
    # (which itself defaults to the main agent's name).
    model_label = settings.model_label or _slugify(rt.name)
    api_keys = set(settings.bridge_api_keys)
    max_upload_bytes = settings.max_upload_bytes

    # In-flight chat counter: the scheduler's idle gate. Scheduled tasks only
    # start when no chat request is being served (they then run concurrently
    # with whatever arrives next — the gate is at start, not for the duration).
    inflight = _InFlight()

    # Hosted MCP surface (MCP_SERVER=true): external clients bring their own
    # model and use this hub's tools/knowledge, authenticated per-user with
    # OWUI API keys. Mounted as a root sub-app below; its lifespan (the
    # Streamable HTTP session manager) is composed into ours.
    mcp_app = None
    if settings.mcp_server:
        from . import mcp_server as mcp_server_lib
        mcp_app = mcp_server_lib.build_mcp_app(hub_dir, settings=settings)

    from contextlib import asynccontextmanager

    from . import scheduler as scheduler_lib

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Connect MCP servers here, in the long-lived lifespan task, so the
        # connection is opened and closed in the SAME task (request handlers
        # run in their own tasks and only *use* the servers). Connecting per
        # request instead raises ClosedResourceError / cancel-scope errors.
        await rt.aopen()
        sched = scheduler_lib.Scheduler(hub_dir, is_busy=inflight.busy)
        sched.start()   # no-op when <hub>/schedule/ is empty or disabled
        app.state.scheduler = sched
        try:
            if mcp_app is not None:
                # Without this the MCP session manager never starts and
                # every /mcp request 500s (the documented FastMCP mounting
                # pitfall).
                async with mcp_app.lifespan(mcp_app):
                    yield
            else:
                yield
        finally:
            await sched.stop()
            await rt.aclose()

    app = FastAPI(title=f"hubzoid · {rt.name}", version="0.1.0", lifespan=_lifespan)
    app.state.runtime = rt

    # In-bridge OTLP normalize intercept (opt-in, claude-local only). When on,
    # the `claude` subprocess exports OTel to this loopback route instead of
    # straight to the backend; we rename Claude Code's non-standard token attrs
    # to the gen_ai.usage.* names Langfuse maps and promote the OWUI user to
    # span user.id, then forward. This makes cost + per-user work on claude-local
    # with NO separate collector process. See hubzoid.otel / docs/OBSERVABILITY.md.
    from . import otel as otellib
    from .factory_claude import ClaudeRuntime
    if settings.otel_normalize and settings.otel_endpoint and isinstance(rt, ClaudeRuntime):
        import httpx
        from fastapi import Response

        _otel_forward_url = settings.otel_endpoint.rstrip("/") + "/v1/traces"
        _otel_headers = otellib.parse_otlp_headers(os.environ.get("OTEL_EXPORTER_OTLP_HEADERS"))

        @app.post("/otel/v1/traces")
        async def _otel_intercept(request: Request):
            raw = await request.body()
            gzipped = request.headers.get("content-encoding", "").lower() == "gzip"
            try:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    status = await otellib.forward_otlp(
                        raw, forward_url=_otel_forward_url, headers=_otel_headers,
                        client=http, gzipped=gzipped,
                    )
                # The upstream (Langfuse) rejecting the batch is silent otherwise
                # — surface it, since it means traces/cost won't appear.
                if status >= 300:
                    log.warning("otel forward -> %s rejected batch: HTTP %s",
                                _otel_forward_url, status)
            except Exception as exc:  # noqa: BLE001 — telemetry is best-effort
                log.warning("otel normalize/forward dropped a batch: %s", exc)
            # Always 200: an OTLP exporter retries non-2xx, and telemetry must
            # never back-pressure or crash the turn that produced it.
            return Response(status_code=200)

    def _auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
        if token not in api_keys:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "hub": hub_dir.name, "agent": rt.name}

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        _auth(request)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_label, "object": "model", "created": int(time.time()), "owned_by": "hubzoid"}
                ],
            }
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        _auth(request)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid json: {exc}")

        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages[] required")

        # Resolve a stable chat id for this request. Falls back to a hash
        # of the first user message so multi-turn chats from clients that
        # don't send an id still get a consistent directory.
        chat_id = _derive_chat_id(body, request, messages)
        identity = _derive_identity(body, request, hub_dir)

        # Extract content[] attachments (base64 image_url / input_file — Slack
        # and direct-API uploads) into the canonical per-chat uploads store now;
        # their notes are prepended after the prompt is assembled below.
        attachment_notes = _persist_attachments(
            hub_dir, chat_id, messages, max_upload_bytes=max_upload_bytes
        )

        prompt = _flatten_messages(messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="empty prompt after flattening messages")

        # Open WebUI uploads arrive as a RAG-wrapped prompt referencing files it
        # stored under .openwebui-data/uploads/. Normalize them into the SAME
        # canonical per-chat uploads store the base64 path just used, so ONE store
        # is authoritative — read_upload, vision and ticket attachments all resolve
        # OWUI files from it, and no downstream reader has to know OWUI exists.
        # Strips OWUI's RAG boilerplate/chunks; pass-through on any non-match.
        owui_uploads = hub_dir / ".openwebui-data" / "uploads"
        if owui_uploads.is_dir():
            normalized = _normalize_owui_uploads(hub_dir, chat_id, prompt, owui_uploads)
            if normalized is not None:
                prompt = normalized

        # Base64/Slack notes go on top, AFTER the OWUI rewrite (which replaces the
        # prompt body), so an image + a document in the same turn both survive.
        if attachment_notes:
            prompt = "\n\n".join(attachment_notes) + "\n\n" + prompt

        if bool(body.get("stream", False)):
            return StreamingResponse(
                _stream(rt, prompt, model_label, chat_id, inflight, identity, hub_dir),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        inflight.enter()
        try:
            with _request_ctx.chat_scope(chat_id):
                with access.identity_scope(identity):
                    text = await rt.run(prompt)
                    usage = _usage_envelope()
        finally:
            inflight.leave()
        return JSONResponse(_blocking_envelope(text, model_label, usage))

    # ------------------------------------------------------------------
    # Per-chat artifact + upload routes.
    # ------------------------------------------------------------------
    @app.get("/artifacts/{chat_id}/{filename:path}")
    async def get_artifact(chat_id: str, filename: str, request: Request):
        # Browsers click links without a Bearer header. We accept either:
        #   * the standard Bearer api key (for curl / SDK callers), OR
        #   * a signed token in `?t=<hex>` (the link Hubzoid writes into
        #     chat by default — see hubzoid._signing).
        safe_chat = _require_safe_chat_id(chat_id)
        safe_name = _safe_path_component(filename)
        token = request.query_params.get("t")
        if not _signing.verify_artifact_token(safe_chat, safe_name, token):
            _auth(request)
        return _serve_chat_file(
            base=memlib.chat_artifact_dir(hub_dir, safe_chat),
            filename=filename,
            inline=True,
        )

    @app.post("/uploads/{chat_id}/{filename:path}")
    async def post_upload(chat_id: str, filename: str, request: Request):
        _auth(request)
        safe_chat = _require_safe_chat_id(chat_id)
        safe_name = _safe_path_component(filename)
        if not safe_name:
            raise HTTPException(status_code=400, detail="invalid filename")
        # Pre-flight on Content-Length so an oversized stream is rejected
        # before we accumulate it in memory. The body-size guard below
        # remains in place for clients that don't set Content-Length.
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeds max_upload_bytes ({max_upload_bytes})",
                    )
            except ValueError:
                pass
        body = await request.body()
        if len(body) > max_upload_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"upload exceeds max_upload_bytes ({max_upload_bytes})",
            )
        upload_dir = memlib.chat_upload_dir(hub_dir, safe_chat)
        target = (upload_dir / safe_name).resolve()
        if upload_dir.resolve() not in target.parents:
            raise HTTPException(status_code=400, detail="filename escapes uploads dir")
        # Pick the mime from Content-Type if the client provided one;
        # otherwise sniff from the filename extension. The sidecar is the
        # authoritative source for `read_upload`'s type-aware preview.
        mime = (request.headers.get("content-type") or "").split(";")[0].strip()
        if not mime or mime == "application/octet-stream":
            mime = uploads_lib.guess_mime(safe_name, fallback=mime or "application/octet-stream")
        target.parent.mkdir(parents=True, exist_ok=True)
        uploads_lib.write_with_meta(upload_dir, safe_name, body, mime=mime)
        return JSONResponse({"chat_id": safe_chat, "filename": safe_name, "size": len(body)})

    if mcp_app is not None:
        # Root-mount, registered last: the sub-app's only route is /mcp, so
        # every real bridge route above matches first and unmatched paths get
        # the sub-app's 404 instead of FastAPI's (cosmetic difference only).
        # Mounting at "/mcp" instead would serve the endpoint at /mcp/ and
        # 307-redirect POST /mcp, which not every MCP client follows.
        app.mount("/", mcp_app)

    return app


# ---------------------------------------------------------------------------
# In-flight tracking (the scheduler's idle gate)
# ---------------------------------------------------------------------------
class _InFlight:
    def __init__(self) -> None:
        self.n = 0

    def enter(self) -> None:
        self.n += 1

    def leave(self) -> None:
        self.n = max(0, self.n - 1)

    def busy(self) -> bool:
        return self.n > 0


# ---------------------------------------------------------------------------
# Streaming response (text/event-stream)
# ---------------------------------------------------------------------------
async def _stream(rt, prompt: str, model: str, chat_id: str,
                  inflight: _InFlight | None = None,
                  identity=None, hub_dir: Path | None = None) -> AsyncIterator[bytes]:
    if inflight:
        inflight.enter()
    try:
        # Role chunk first (OpenAI convention).
        first = _chunk("", model=model)
        first["choices"][0]["delta"] = {"role": "assistant", "content": ""}
        yield f"data: {json.dumps(first)}\n\n".encode()

        # Set chat scope so tools resolve to this chat's dirs, and bind the
        # caller's identity so the access guard sees who is running each tool.
        usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        with _request_ctx.chat_scope(chat_id), access.identity_scope(identity):
            async for delta in rt.stream(prompt):
                if delta:
                    yield f"data: {json.dumps(_chunk(delta, model=model))}\n\n".encode()
            # Drain usage while still inside chat_scope (the runtime set it
            # there); build the OpenAI usage envelope for the final chunk.
            usage = _usage_envelope()

        yield f"data: {json.dumps(_chunk(None, finish_reason='stop', model=model))}\n\n".encode()
        # Final usage chunk (OpenAI `stream_options.include_usage` convention):
        # empty choices + top-level usage. Open WebUI reads this to populate its
        # native per-message token stats (previously it received zeros).
        usage_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": usage,
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    finally:
        if inflight:
            inflight.leave()


def _usage_envelope() -> dict:
    """Drain the turn's usage and return the OpenAI usage envelope
    (prompt/completion/total tokens) that populates Open WebUI's native
    per-message token column. Must be called while the request's chat_scope is
    still active. Cost/usage analytics itself is emitted via OTel (opt-in,
    see hubzoid.otel); this envelope is the one piece that needs no backend."""
    raw = _request_ctx.drain_usage()

    def _n(key: str) -> int:
        # Never raise: a malformed usage value must not 500 the chat response.
        try:
            return int(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    inp, out = _n("input_tokens"), _n("output_tokens")
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}


# ---------------------------------------------------------------------------
# Identity derivation
# ---------------------------------------------------------------------------
def _derive_identity(body: dict[str, Any], request: Request, hub_dir: Path | None = None):
    """Resolve the caller's verified identity for this request.

    The user, groups, and surface come from headers the trusted front sets;
    they are trusted because reaching the bridge already requires its API key
    (``_auth``), and end users talk to Open WebUI, never to the bridge directly.

    Groups are the UNION of two sources, so an identity can carry both (see
    ``merge_group_sources``):

      1. Open WebUI's forwarded ``X-OpenWebUI-User-Email`` — the bridge looks up
         that user's groups in OWUI's own database (where the admin manages them
         on the Groups screen). This is what makes adding a person to a group in
         Open WebUI grant the matching permission, with no proxy and no logout.
      2. ``X-Hubzoid-Groups`` (comma-separated) — groups supplied by a surface
         resolver or trusted front (e.g. the WhatsApp/Telegram roster), merged on
         top of the OWUI groups rather than overriding them.

    A request with no groups reaches no restricted tool (fail-closed). A
    non-Open-WebUI surface (e.g. Slack, which sets ``X-Hubzoid-Surface: slack``)
    is refused restricted tools regardless. ``body["user"]`` is only a display
    fallback for the user id; it never carries groups.
    """
    headers = request.headers
    owui_email = headers.get("x-openwebui-user-email")
    user = headers.get("x-hubzoid-user") or owui_email
    if not user:
        u = body.get("user")
        user = u if isinstance(u, str) and u.strip() else None

    # Groups MERGE (union), so an identity can carry both its Open WebUI groups
    # and groups supplied by a surface resolver / trusted front. Non-breaking:
    # an OWUI-only request still gets exactly its OWUI groups, a header-only
    # request still gets exactly its header groups; only when BOTH are present
    # (the WhatsApp/Telegram path) do they combine instead of one overriding.
    groups_raw = headers.get("x-hubzoid-groups")
    owui_groups = (
        access.owui_groups.resolve_groups(hub_dir, owui_email)
        if owui_email and hub_dir is not None else set()
    )
    groups = merge_group_sources(owui_groups, groups_raw)

    surface = headers.get("x-hubzoid-surface") or "owui"
    return access.Identity.make(user=user, groups=groups, surface=surface)


def merge_group_sources(owui_groups, header_groups_raw) -> list:
    """Union an identity's group sources: Open WebUI groups + comma-separated
    header groups (from a surface resolver or trusted front). Blank entries are
    dropped; final normalization/dedup is applied by `access.Identity.make`.
    """
    groups = set(owui_groups or ())
    if header_groups_raw is not None:
        groups |= {g.strip() for g in header_groups_raw.split(",") if g.strip()}
    return list(groups)


# ---------------------------------------------------------------------------
# Chat-id derivation
# ---------------------------------------------------------------------------
def _derive_chat_id(body: dict[str, Any], request: Request, messages: list) -> str:
    """Pick the most specific chat id available, sanitised.

    Tried in order:
      1. body["chat_id"]
      2. body["metadata"]["chat_id"]              (Open WebUI shape)
      3. request header X-Hubzoid-Chat-Id
      4. body["user"]                             (OpenAI's user field, sometimes set)
      5. hash of the first user message text      (stable fallback)

    Returns a non-empty string suitable for a path component.
    """
    candidates = []
    chat_id = body.get("chat_id")
    if chat_id:
        candidates.append(chat_id)
    meta = body.get("metadata") or {}
    if isinstance(meta, dict):
        candidates.append(meta.get("chat_id"))
        candidates.append(meta.get("conversation_id"))
    candidates.append(request.headers.get("x-hubzoid-chat-id"))
    candidates.append(body.get("user"))

    for raw in candidates:
        safe = memlib.sanitize_chat_id(raw)
        if safe:
            return safe

    # Fallback: derive from the first user message. Stable across turns
    # because the first message is replayed every turn by OpenAI clients.
    first_user = next((m for m in messages if m.get("role") == "user"), None) or {}
    text = _stringify_content(first_user.get("content") or "")
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"hash-{h}"


def _require_safe_chat_id(raw: str) -> str:
    safe = memlib.sanitize_chat_id(unquote(raw))
    if not safe:
        raise HTTPException(status_code=400, detail="invalid chat_id")
    return safe


# ---------------------------------------------------------------------------
# Attachment extraction
# ---------------------------------------------------------------------------
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[^;,]+)(?:;name=(?P<name>[^;,]+))?(?:;[^,]*)?;base64,(?P<data>.+)$",
    re.DOTALL,
)


def _attachment_note(safe_name: str, size: int, mime: str, target: Path) -> str:
    """The one canonical prompt marker for an ingested upload, shared by the
    base64/direct-API path and the Open WebUI path so both produce identical
    downstream shape. Images get the `[Image: ...]` reference that
    `hubzoid.vision_inject` expands into a multimodal content block at model-call
    time (the model sees the image directly — no read_upload needed). Everything
    else points the agent at `read_upload` AND carries the on-disk path, so
    path-accepting tools and shell scripts (e.g. a hub's `test_template.py <file>`)
    can be handed the file directly — that path was part of the old OWUI note and
    some hubs depend on it."""
    if mime.startswith("image/"):
        return f"[Image: {safe_name}]  (attached image, shown to you directly)"
    return (
        f"[User attached file: {safe_name} ({size} bytes, {mime}). "
        f"Read it with read_upload('{safe_name}'), or pass its on-disk path to a "
        f"path-accepting tool or script: {target}]"
    )


def _normalize_owui_uploads(
    hub_dir: Path, chat_id: str, prompt: str, owui_uploads: Path
) -> str | None:
    """Copy every file Open WebUI attached into the ONE canonical per-chat uploads
    store, then return the prompt rewritten to the same attachment-note shape a
    Slack / base64 upload produces.

    Returns None when the prompt is not an OWUI RAG wrap (caller passes it through
    untouched). When OWUI referenced files but some or all could not be read
    (missing on disk, template drift), those become a LOUD, visible note rather
    than a silent drop — a silent drop is exactly what makes a user paste a whole
    file into chat.
    """
    result = owui_lib.owui_attachments(prompt, owui_uploads)
    if result is None:
        return None
    resolved, unresolved, user_query = result

    upload_dir = memlib.chat_upload_dir(hub_dir, chat_id)
    notes: list[str] = []
    for name, src in resolved:
        safe_name = _safe_path_component(name)
        if not safe_name:
            continue
        try:
            payload = src.read_bytes()
        except OSError as exc:  # unreadable on disk -> treat as unresolved, loudly
            log.warning("owui: could not read attached file %s: %s", src, exc)
            unresolved.append(name)
            continue
        mime = uploads_lib.guess_mime(safe_name)
        uploads_lib.write_with_meta(upload_dir, safe_name, payload, mime=mime)
        notes.append(_attachment_note(safe_name, len(payload), mime, upload_dir / safe_name))

    if unresolved:
        listed = ", ".join(sorted(set(unresolved)))
        log.warning(
            "owui: %d attached file(s) referenced but not readable under %s: %s",
            len(set(unresolved)), owui_uploads, listed,
        )
        notes.append(
            f"[Attachment unreadable: the user attached {listed} but it could not be "
            f"read from storage. Tell them and ask them to re-upload it or paste the "
            f"relevant part. Do NOT guess its contents.]"
        )

    body = "\n\n".join(notes)  # owui_attachments returns non-None only with >=1 ref
    return f"{body}\n\n{user_query}" if user_query else body


def _persist_attachments(
    hub_dir: Path,
    chat_id: str,
    messages: list,
    *,
    max_upload_bytes: int,
) -> list[str]:
    """Scan message content for image/file attachments, write to uploads dir.

    Returns a list of human-readable notes to prepend to the prompt so the
    agent knows what was attached and how to read it.

    Raises HTTPException(413) if any single attachment decodes to more than
    `max_upload_bytes`. A partial write would leave the chat with a
    half-attachment the agent could still call `read_upload` on; we'd
    rather refuse the whole turn than let the model reason over corrupt
    state.
    """
    # Decode + validate every attachment first; only commit to disk once
    # we know all of them fit under the cap. Avoids creating the chat's
    # uploads dir at all when the request is going to be rejected.
    decoded: list[tuple[str, bytes, str]] = []  # (safe_name, payload, mime)
    counter = 0

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            data_url: str | None = None
            suggested_name: str | None = None
            if kind == "image_url":
                url_obj = block.get("image_url") or {}
                url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
                if isinstance(url, str) and url.startswith("data:"):
                    data_url = url
            elif kind == "input_file" or kind == "file":
                # Some clients use this shape. The actual url field name varies.
                for key in ("data", "url", "file_url"):
                    val = block.get(key)
                    if isinstance(val, dict):
                        val = val.get("url")
                    if isinstance(val, str) and val.startswith("data:"):
                        data_url = val
                        break
                suggested_name = block.get("name") or block.get("filename")

            if not data_url:
                continue
            match = _DATA_URL_RE.match(data_url)
            if not match:
                continue
            mime = match.group("mime") or "application/octet-stream"
            data_b64 = match.group("data")
            # Cheap upper bound: base64 expands by ~4/3, so the decoded
            # size is at most len(b64) * 3 / 4. If even that lower bound
            # exceeds the cap, reject before decoding the (possibly huge)
            # payload — protects against amplification.
            if (len(data_b64) * 3) // 4 > max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"attachment exceeds max_upload_bytes ({max_upload_bytes})",
                )
            try:
                payload = base64.b64decode(data_b64, validate=True)
            except Exception:  # noqa: BLE001
                continue
            if len(payload) > max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"attachment exceeds max_upload_bytes ({max_upload_bytes})",
                )
            counter += 1
            ext = (mimetypes.guess_extension(mime) or "").lstrip(".") or "bin"
            safe_name = _safe_path_component(suggested_name or match.group("name") or f"upload-{counter}.{ext}")
            if not safe_name:
                safe_name = f"upload-{counter}.{ext}"
            decoded.append((safe_name, payload, mime))

    if not decoded:
        return []

    upload_dir = memlib.chat_upload_dir(hub_dir, chat_id)
    notes: list[str] = []
    for safe_name, payload, mime in decoded:
        uploads_lib.write_with_meta(upload_dir, safe_name, payload, mime=mime)
        notes.append(_attachment_note(safe_name, len(payload), mime, upload_dir / safe_name))
    return notes


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return str(content)


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------
def _serve_chat_file(*, base: Path, filename: str, inline: bool) -> FileResponse:
    safe_name = _safe_path_component(unquote(filename))
    if not safe_name:
        raise HTTPException(status_code=400, detail="invalid filename")
    target = (base / safe_name).resolve()
    if base.resolve() not in target.parents:
        raise HTTPException(status_code=400, detail="filename escapes directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media_type, _ = mimetypes.guess_type(target.name)
    disp = "inline" if inline else "attachment"
    headers = {"Content-Disposition": f'{disp}; filename="{safe_name}"'}
    # Agent-authored artifacts (e.g. a dashboard HTML built by the `dashboard`
    # skill) are served inline and may be on the same origin as Open WebUI
    # behind the edge proxy. Sandbox them: a CSP `sandbox` with NO
    # `allow-same-origin` forces a unique opaque origin, so any script embedded
    # in the artifact cannot read OWUI's session/JWT or cookies. That opaque
    # origin is the ONLY control that matters here — withholding `allow-forms`
    # etc. adds no security (a script can already exfiltrate via fetch), so we
    # grant the capabilities interactive artifacts legitimately need (charts,
    # forms, in-tab links, downloads) while keeping `allow-same-origin` off.
    if inline:
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-popups allow-downloads "
            "allow-forms allow-top-navigation-by-user-activation"
        )
    return FileResponse(
        path=str(target),
        media_type=media_type or "application/octet-stream",
        headers=headers,
    )


def _safe_path_component(raw: str | None) -> str:
    if not raw:
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = name.replace("\x00", "")
    if name in ("", ".", ".."):
        return ""
    return name


# ---------------------------------------------------------------------------
# OpenAI envelope helpers
# ---------------------------------------------------------------------------
def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(
                c.get("text", "") if isinstance(c, dict) else str(c) for c in content
            )
        if not content:
            continue
        if role == "system":
            parts.insert(0, f"[system]\n{content}")
        elif role == "assistant":
            parts.append(f"[assistant]\n{content}")
        else:
            parts.append(f"[user]\n{content}")
    return "\n\n".join(parts).strip()


def _chunk(content_delta: str | None, *, finish_reason: str | None = None, model: str) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content_delta is not None:
        delta["content"] = content_delta
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _blocking_envelope(text: str, model: str, usage: dict | None = None) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _slugify(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "agent"


# Uvicorn entry point when launched as a separate process.
def _factory():
    return build_app()


app = None  # populated when run via `uvicorn hubzoid.server:app --factory` not needed; we use _factory pattern in cli.py
