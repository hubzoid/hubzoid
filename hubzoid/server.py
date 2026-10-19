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

import asyncio

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
from . import browser as browser_lib
from . import memory as memlib
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
    logging.getLogger("alembic").setLevel(
        logging.DEBUG if settings.log_level.lower() == "debug" else logging.WARNING)
    # A hub in a gateway's deployment serves only in the mode and sign-in the
    # gateway recorded; settings that disagree stop the bridge before it serves.
    from . import appmode

    conflicts = appmode.deployment_conflicts(hub_dir)
    if conflicts:
        raise RuntimeError(" ".join(conflicts))
    # The rest of the retired Open WebUI settings change nothing; name them, as
    # `hubzoid run` and `hubzoid doctor` do, for a bridge started on its own.
    for retired in appmode.retired_settings(hub_dir):
        if not retired.fatal:
            log.warning("%s is an Open WebUI setting Hubzoid 2.0 no longer uses. %s",
                        retired.name, retired.advice)
    # Bring Hubzoid's own tables to the current schema before serving anything;
    # a schema this version can't use stops the bridge here, loudly.
    from . import db as dblib
    from . import migrations

    migrations.upgrade(dblib.operational_engine(hub_dir), "operational")
    migrations.upgrade(dblib.engine_for(hub_dir), "hub")
    # Second phase: secrets stored in the Console fill the keys no operator
    # source sets, read once now and never changed while serving.
    from .secretstore import layer as secret_layer

    secret_layer.enable(hub_dir)
    settings = settingslib.load(hub_dir)
    rt = runtime_lib.build(hub_dir)

    # Model label shown in /v1/models. Falls back to the runtime's name
    # (which itself defaults to the main agent's name).
    model_label = settings.model_label or _slugify(rt.name)
    api_keys = set(settings.bridge_api_keys)
    if "dev" in api_keys:
        log.warning(
            "BRIDGE_API_KEYS includes the public default key 'dev'. Set a random "
            "key in the hub's .env for any shared or deployed install."
        )
    max_upload_bytes = settings.max_upload_bytes

    # In-flight chat counter: the scheduler's idle gate. Scheduled tasks only
    # start when no chat request is being served (they then run concurrently
    # with whatever arrives next — the gate is at start, not for the duration).
    inflight = _InFlight()

    # Hosted MCP surface (MCP_SERVER=true): external clients bring their own
    # model and use this hub's tools/knowledge, authenticated per-user. Mounted as a root sub-app below; its lifespan (the
    # Streamable HTTP session manager) is composed into ours.
    mcp_app = None
    if settings.mcp_server:
        from . import mcp_server as mcp_server_lib
        mcp_app = mcp_server_lib.build_mcp_app(hub_dir, settings=settings)

    from contextlib import asynccontextmanager

    from . import scheduler as scheduler_lib

    # Shared browser sidecar (HUBZOID_BROWSER=true). Inert otherwise. Started
    # BEFORE rt.aopen() so the auto-injected `playwright` MCP server has a live
    # endpoint to connect to; stopped after rt.aclose().
    browser_mgr = browser_lib.build(settings)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Connect MCP servers here, in the long-lived lifespan task, so the
        # connection is opened and closed in the SAME task (request handlers
        # run in their own tasks and only *use* the servers). Connecting per
        # request instead raises ClosedResourceError / cancel-scope errors.
        await browser_mgr.start()
        await rt.aopen()
        # All scheduled work runs on the hub's DBOS engine: markdown
        # schedule/*.md tasks and scheduled evals (whenever their files exist)
        # and code workflows (when the box is marked: HUBZOID_SCHEDULES /
        # gateway). The engine starts first; the markdown scheduler then decides
        # when each task is due and queues it there. hub.call_llm/call_agent/
        # call_jev reach this hub's runtime through the seams below.
        from . import runtime as _agent_rt
        from .workflows import boot as wf_boot
        from .workflows import context as wf_ctx
        wf_ctx.configure(
            llm=lambda spec, hub_dir=None, subject=None: _agent_rt.complete_once(hub_dir, spec, subject=subject),
            agent=lambda task, hub_dir=None, subject=None: _agent_rt.run_once(hub_dir, task, subject=subject),
            jev=lambda spec, hub_dir=None, subject=None: _agent_rt.jev_once(hub_dir, spec, subject=subject),
        )
        wf_dispatcher = await wf_boot.start(hub_dir)
        app.state.workflows = wf_dispatcher
        from .workflows import runtime as _wf_runtime
        sched = scheduler_lib.Scheduler(hub_dir, is_busy=inflight.busy)
        if _wf_runtime._LAUNCHED or wf_dispatcher is not None:
            # Also while the engine waits for ownership: a slot that cannot be
            # queued yet is not stamped, so it fires once the engine is back.
            sched.start()   # no-op when <hub>/schedule/ is empty or disabled
        elif wf_boot.markdown_work(hub_dir):
            log.error("scheduled markdown tasks are not running: the workflow engine "
                      "did not start (see the error above)")
        app.state.scheduler = sched
        chat_ctx = getattr(app.state, "chat", None)
        from .hitl import routes as hitl_routes
        hitl_task = asyncio.create_task(hitl_routes.recovery_loop(chat_ctx)) if chat_ctx is not None else None
        if chat_ctx is not None:
            from .hub_changes import service as hub_changes

            try:  # a hub change a crash left half applied: finish it, one commit
                await asyncio.to_thread(hub_changes.recover, hub_dir)
            except Exception:  # noqa: BLE001
                log.exception("hub changes: recovery failed")
        from .sandbox import service as sandbox_service
        reaper_task = asyncio.create_task(sandbox_service.reaper_loop(hub_dir))  # idle when sandboxes are off
        # Channels: route provider deliveries saved before a restart, post
        # replies that were due, and start Socket Mode or mailbox transports.
        from .channels import router as channel_router
        try:
            await channel_router.start(app, hub_dir)
        except Exception:  # noqa: BLE001 - channels never stop the bridge from serving
            log.exception("channels: could not start routing")
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
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass
            if hitl_task is not None:
                hitl_task.cancel()
            await channel_router.stop()
            if wf_dispatcher is not None:
                await wf_dispatcher.stop()
            await sched.stop()
            await rt.aclose()
            await browser_mgr.stop()

    app = FastAPI(title=f"hubzoid · {rt.name}", version="0.1.0", lifespan=_lifespan)
    app.state.runtime = rt

    # In-bridge OTLP normalize intercept (opt-in, claude-local only). When on,
    # the `claude` subprocess exports OTel to this loopback route instead of
    # straight to the backend; we rename Claude Code's non-standard token attrs
    # to the gen_ai.usage.* names Langfuse maps and promote the user to
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

    def _auth(request: Request, *, personal: bool = False) -> None:
        """The bridge's own keys, or (``personal``: the chat API only) a
        person's API key. Files, uploads and artifacts take bridge keys only:
        a person's key reaches them through the chat API's own checks."""
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
        if token.startswith("hzp_") and not personal:
            raise HTTPException(status_code=401, detail="invalid api key")
        if token.startswith("hzp_") and hub_dir is not None:
            # A person's own API key: the call runs as them, within the key's
            # limits (``hubzoid.auth.api_keys``), not as the trusted front.
            from .auth import sessions as sessionlib

            user = sessionlib.resolve(request, hub_dir)
            if user is None:
                raise HTTPException(status_code=401, detail="invalid api key")
            request.state.hz_key_user = user
            return
        if token not in api_keys:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "hub": hub_dir.name, "agent": rt.name, "model": model_label}

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        _auth(request, personal=True)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_label, "name": rt.name, "object": "model", "created": int(time.time()), "owned_by": "hubzoid"}
                ],
            }
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        _auth(request, personal=True)
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
        key_user = getattr(request.state, "hz_key_user", None)
        if key_user is not None:
            # The client's chat id is only a label within the key owner's own
            # space: it can't select another person's (or chat's) files.
            chat_id = _personal_chat_id(key_user.id, chat_id)
            identity = await asyncio.to_thread(_key_identity, hub_dir, key_user, chat_id)
        else:
            identity = _derive_identity(body, request, hub_dir)
            # Hub-entry gate: a caller must hold `use_hub` (verified identity) to
            # chat at all — read knowledge, use even unrestricted tools. Fail-closed.
            _enforce_use_hub(request, hub_dir)
            if identity.user and hub_dir is not None:
                identity = await asyncio.to_thread(_channel_identity, hub_dir, identity, chat_id)

        # Extract content[] attachments (base64 image_url / input_file — Slack
        # and direct-API uploads) into the canonical per-chat uploads store now;
        # their notes are prepended after the prompt is assembled below.
        attachment_notes = _persist_attachments(
            hub_dir, chat_id, messages, max_upload_bytes=max_upload_bytes
        )

        prompt = _flatten_messages(messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="empty prompt after flattening messages")

        # Base64/Slack notes go on top of the prompt.
        if attachment_notes:
            prompt = "\n\n".join(attachment_notes) + "\n\n" + prompt

        if bool(body.get("stream", False)):
            return StreamingResponse(
                _stream(rt, prompt, model_label, chat_id, inflight, identity, hub_dir),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        inflight.enter()
        started = time.monotonic()
        try:
            with _request_ctx.chat_scope(chat_id):
                with access.identity_scope(identity):
                    text = await rt.run(prompt)
                    raw_usage = _request_ctx.drain_usage()
        finally:
            inflight.leave()
        if request.headers.get("x-hubzoid-task", "").strip():
            raw_usage = {**raw_usage, "kind": "background"}
        await _record_turn(hub_dir, identity, chat_id, raw_usage, started)
        return JSONResponse(_blocking_envelope(text, model_label, _usage_envelope(raw_usage)))

    # ------------------------------------------------------------------
    # Per-chat artifact + upload routes.
    # ------------------------------------------------------------------
    from . import appmode

    if not appmode.auth_enabled(hub_dir):
        # Sign-in off: the local owner is the only person, and owns every hub
        # from the first start, before any page is opened. Best effort: a store
        # problem never stops the bridge, whose access checks fail closed.
        from .access.session import LOCAL_OWNER
        from .auth.users import provision_owner

        try:
            provision_owner(hub_dir, LOCAL_OWNER)
        except Exception:  # noqa: BLE001
            log.warning("access: the local owner could not be provisioned yet", exc_info=True)

    @app.get("/artifacts/{chat_id}/{filename:path}")
    async def get_artifact(chat_id: str, filename: str, request: Request):
        # Browsers click links without a Bearer header. We accept either:
        #   * a signed token in `?t=<hex>` (the link Hubzoid writes into
        #     chat by default — see hubzoid._signing), OR
        #   * in the web app, the signed-in owner of that conversation (their
        #     session cookie), so an expired link still works for them, OR
        #   * a real Bearer api key (for curl / SDK callers). This route is
        #     public behind the edge, so the default "dev" key is not accepted.
        safe_chat = _require_safe_chat_id(chat_id)
        safe_name = _safe_path_component(filename)
        token = request.query_params.get("t")
        expires = request.query_params.get("e")
        if not _signing.verify_artifact_token(
            safe_chat, safe_name, token, expires, hub_dir=hub_dir
        ) and not (await asyncio.to_thread(
            _owns_conversation, request, hub_dir, safe_chat
        )):
            auth = request.headers.get("authorization", "")
            if auth[7:].strip() == "dev" and auth.lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="invalid or expired link")
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

    # Published artifacts (viewer, sharing, public links) under /portal, so any
    # bridge serves them through the edge's /portal route. Before the portal's
    # static mount, which would otherwise answer these paths.
    from .artifacts import web as artifacts_web

    app.include_router(artifacts_web.build_router(hub_dir))
    from .workflows.webhooks import build_router as workflow_webhooks
    app.include_router(workflow_webhooks(hub_dir))

    # The Hubzoid web app: sign-in, conversations, personal connections,
    # agents and the page shell. Registered before the portal's static mount so
    # /portal/api/... routes it adds are not shadowed.
    from . import webapp

    webapp.mount(app, hub_dir, runtime=rt, inflight=inflight, settings=settings,
                 model_label=model_label)

    # Admin portal: JSON API under /portal/api + the static SPA at /portal.
    # Registered before the root MCP mount so /portal is not swallowed.
    try:
        from . import portal as portal_lib

        portal_lib.mount_portal(app, hub_dir)
    except Exception:  # noqa: BLE001 — the portal is optional; never block the bridge
        logging.getLogger("hubzoid.portal").exception("portal mount failed")

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
    started = time.monotonic()
    shown = None  # the status line on screen ("Working on it…", "Running X…"), or None
    try:
        # Role chunk first (OpenAI convention).
        first = _chunk("", model=model)
        first["choices"][0]["delta"] = {"role": "assistant", "content": ""}
        yield f"data: {json.dumps(first)}\n\n".encode()
        # The first words can take seconds (runtime start-up, first token), and
        # the chat app's own cue for that is faint. Show its status line until
        # the first content arrives, then hide it. Status is message metadata,
        # never message content.
        shown = _WAITING
        yield _status_event(model, shown)

        # Set chat scope so tools resolve to this chat's dirs, and bind the
        # caller's identity so the access guard sees who is running each tool.
        usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        from .tool_events import THINKING, Status

        with _request_ctx.chat_scope(chat_id), access.identity_scope(identity):
            async for delta in rt.stream(prompt):
                if isinstance(delta, Status):
                    # A running tool's line ("Running X…"), or None to clear it.
                    if delta.description != shown:
                        shown = delta.description
                        yield _status_event(model, shown)
                    continue
                if delta:
                    if shown in (_WAITING, THINKING):  # the answer has started
                        shown = None
                        yield _status_event(model, None)
                    yield f"data: {json.dumps(_chunk(delta, model=model))}\n\n".encode()
            # Drain usage while still inside chat_scope (the runtime set it
            # there); build the OpenAI usage envelope for the final chunk.
            raw_usage = _request_ctx.drain_usage()
            usage = _usage_envelope(raw_usage)
        await _record_turn(hub_dir, identity, chat_id, raw_usage, started)

        if shown is not None:
            shown = None
            yield _status_event(model, None)
        yield f"data: {json.dumps(_chunk(None, finish_reason='stop', model=model))}\n\n".encode()
        # Final usage chunk (OpenAI `stream_options.include_usage` convention):
        # empty choices + top-level usage, for clients that show token counts.
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
    except Exception:
        # A failed turn must not leave a status line behind its error.
        if shown is not None:
            yield _status_event(model, None)
        raise
    finally:
        if inflight:
            inflight.leave()


_WAITING = "Working on it…"


def _waiting_status(model: str, *, done: bool) -> bytes:
    """A status event (top-level `event` on a chunk with no choices) on the
    OpenAI-compatible stream. Clients that know it show a status line; others
    see an empty chunk, like the usage chunk."""
    return _status_event(model, None if done else _WAITING)


def _status_event(model: str, description: str | None) -> bytes:
    """The chat app's status line: `description` while work runs, hidden when
    None (the line disappears; the chat app keeps the history as metadata)."""
    data = {"description": description or _WAITING, "done": description is None}
    if description is None:
        data["hidden"] = True
    chunk = {"object": "chat.completion.chunk", "created": int(time.time()), "model": model,
             "choices": [], "event": {"type": "status", "data": data}}
    return f"data: {json.dumps(chunk)}\n\n".encode()


async def _record_turn(hub_dir, identity, chat_id, raw: dict, started: float) -> None:
    """Write this chat turn's usage row (hubzoid.usage), off the event loop."""
    if hub_dir is None:
        return
    from . import usage as usage_lib

    def _int(key):
        try:
            return int(raw.get(key)) if raw.get(key) is not None else None
        except (TypeError, ValueError):
            return None

    await asyncio.to_thread(
        usage_lib.record, hub_dir,
        hub=Path(hub_dir).name,
        surface=getattr(identity, "surface", None) or "api",
        kind=raw.get("kind", "chat"),
        subject=getattr(identity, "user", None),
        chat_id=chat_id,
        model=raw.get("model"),
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cost_usd=raw.get("cost_usd"),
        status=raw.get("status") or "ok",
        duration_ms=int((time.monotonic() - started) * 1000),
        reservation=raw.get("budget_reservation"),
    )


def _usage_envelope(raw: dict) -> dict:
    """The OpenAI usage envelope (prompt/completion/total tokens) from a turn's
    drained usage; it populates the chat UI's per-message token column. The
    Console's own numbers come from hubzoid.usage rows, not from this."""

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
_UNSET = object()


def _trust(request: Request, hub_dir: Path | None):
    """What a valid `X-Hubzoid-Assertion` vouches for on this request
    (`hubzoid.assertions`), computed once; None for an anonymous request.
    Identity headers count only as far as the assertion covers them."""
    state = getattr(request, "state", None)
    cached = getattr(state, "hubzoid_trust", _UNSET) if state is not None else _UNSET
    if cached is not _UNSET:
        return cached
    from . import assertions

    vouched = assertions.vouched(hub_dir, request.headers)
    if state is not None:
        try:
            state.hubzoid_trust = vouched
        except AttributeError:
            pass
    return vouched


def _enforce_use_hub(request: Request, hub_dir: Path | None):
    """Require `use_hub` to enter the hub at all.

    Uses ONLY the verified identity — never the caller-controlled `body.user` —
    so a hub cannot be entered anonymously or under a spoofed subject: the
    email a valid identity assertion vouches for. Fail-closed: a store error
    denies (503)."""
    if hub_dir is None:
        return
    from .access import store_for
    from .access.store import USE_HUB

    try:
        gs = store_for(hub_dir)
    except Exception:  # noqa: BLE001
        log.exception("access: use_hub check unavailable for %s", hub_dir.name)
        raise HTTPException(status_code=503, detail="access check unavailable")
    vouched = _trust(request, hub_dir)
    verified = vouched.email if vouched is not None else ""
    try:
        blocked = bool(verified and gs.is_suspended(verified))
    except Exception:
        raise HTTPException(503, detail="access check unavailable")
    if blocked:
        raise HTTPException(403, detail="Your agent access is blocked. Contact your administrator.")
    if not verified:
        raise HTTPException(
            status_code=403,
            detail=f"The '{hub_dir.name}' hub requires sign-in.",
        )
    try:
        ok = gs.can(verified, hub_dir.name, USE_HUB)
    except Exception:  # noqa: BLE001
        log.exception("access: use_hub can() failed for %s", hub_dir.name)
        raise HTTPException(status_code=503, detail="access check unavailable")
    if not ok:
        raise HTTPException(
            status_code=403,
            detail=(
                f"You do not have access to the '{hub_dir.name}' hub. "
                f"Signed in as {verified}. "
                + ("Open Console → Agents → Access to review your chat permission."
                   if gs.can(verified, hub_dir.name, "manage_access") else
                   "Ask your hub administrator for chat access, then start a new chat.")
            ),
        )


def _key_identity(hub_dir: Path, user, chat_id: str):
    """A ``/v1`` call made with a personal API key: the key's owner may use
    this hub (``access.evaluator.authorize``), and the call gets its run
    identity (started from the API, no one watching)."""
    from .access import Identity, run_identity
    from .access.evaluator import authorize
    from .access.store import USE_HUB

    decision = authorize(hub_dir, user, USE_HUB, hub=hub_dir.name)
    if not decision.allowed:
        status = 503 if decision.reason == "store_error" else 403
        raise HTTPException(status_code=status, detail="this API key's owner may not use this agent")
    try:
        record = run_identity.admit_or_refuse(
        hub_dir, run_kind="api", run_id=f"v1:{uuid.uuid4().hex}", acts_as_id=user.id,
        acts_as_email=user.email, started_from="api", origin_id=chat_id, person_present=False,
        credential_kind="api_key", credential_id=user.credential_id,
        scope={"hubs": list(user.hubs)} if user.hubs is not None else None)
    except run_identity.NotAdmitted as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return run_identity.identity(record, "api")


def _derive_identity(body: dict[str, Any], request: Request, hub_dir: Path | None = None):
    """Resolve the caller's verified identity for this request.

    The user, groups and surface count only when a valid
    ``X-Hubzoid-Assertion`` covers exactly their values
    (``hubzoid.assertions``). Groups are then the hub roster's
    (``identity/access.{csv,py}``) and the asserted ones (``X-Hubzoid-Groups``),
    merged by ``access.effective_groups``. Groups describe the person; they
    authorize nothing: restricted tools need a grant in the access store, and a
    surface that does not verify a person (Slack) is refused them regardless.
    Without an assertion the request is anonymous on the ``api`` surface, and
    ``body["user"]`` (caller-controlled) is never an identity.
    """
    vouched = _trust(request, hub_dir)
    if vouched is None:
        return access.Identity.make(user=None, groups=None, surface="api")
    email = vouched.email or None
    surface = vouched.surface or "api"
    groups = access.effective_groups(
        hub_dir, email=email, surface=surface, header_groups=list(vouched.groups),
    )
    return access.Identity.make(user=email, groups=groups, surface=surface)


# ---------------------------------------------------------------------------
# Chat-id derivation
# ---------------------------------------------------------------------------
def _channel_identity(hub_dir: Path, identity, chat_id: str):
    """A verified person writing from a channel (WhatsApp or Telegram, matched
    to their account by phone; Slack): the run's identity record, so each tool
    call checks that account's state and credential counter."""
    import uuid

    from .access import run_identity

    try:
        record = run_identity.admit_or_refuse(
            Path(hub_dir), run_kind="webhook", run_id=f"{identity.surface}:{uuid.uuid4().hex}",
            acts_as_email=identity.user, started_from="webhook", origin_id=chat_id,
            person_present=True, credential_kind=identity.surface)
    except run_identity.NotAdmitted as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return run_identity.identity(record, identity.surface, identity.groups)


def _personal_chat_id(account_id: str, client_chat_id: str) -> str:
    """A personal API key's chat space: the account plus the client's id."""
    digest = hashlib.sha256(f"{account_id}\0{client_chat_id}".encode("utf-8")).hexdigest()
    return "key-" + digest[:32]


def _derive_chat_id(body: dict[str, Any], request: Request, messages: list) -> str:
    """Pick the most specific chat id available, sanitised.

    Tried in order:
      1. body["chat_id"]
      2. body["metadata"]["chat_id"]
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


def _owns_conversation(request: Request, hub_dir: Path, chat_id: str) -> bool:
    """True when the signed-in person (web app session) owns the conversation
    whose files live under this chat id (its ``chat.store.chat_key``: ``web-<id>``,
    or the Open WebUI id of an imported one). Another surface's chat (Slack,
    Telegram, WhatsApp) is never a conversation's,
    even when a conversation has the same id. Any failure is a no."""
    try:
        from . import auth
        from .chat import store as chat_store

        user = auth.current_user(request, hub_dir)
        if user is None:
            return False
        conv = chat_store.for_hub(hub_dir).conversation_for_chat_key(chat_id)
        return (conv is not None and conv["owner_id"] == user.id
                and conv["hub"] == Path(hub_dir).name.lower())
    except Exception:  # noqa: BLE001 — fall back to the token and key checks
        log.warning("artifacts: session check failed", exc_info=True)
        return False


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
    # skill) are served inline and may be on the same origin as the web app
    # behind the edge proxy. Sandbox them: a CSP `sandbox` with NO
    # `allow-same-origin` forces a unique opaque origin, so any script embedded
    # in the artifact cannot read the web app's session or cookies. That opaque
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
