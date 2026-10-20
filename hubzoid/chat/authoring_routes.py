"""Routes for Workspace authoring (``hubzoid.chat.authoring``). Changes to the
files are hub changes (``POST /api/hub-changes``), proposed and approved there.

  GET    /api/chat/authoring/files                       authorable hub files and their hashes
  GET    /api/chat/authoring/files/content?path=         one file's text
  GET    /api/chat/authoring/files/versions?path=        one file's Git history
  POST   /api/chat/authoring/openapi/preview             {spec, selected?} -> operations; tools for the selected
  GET    /api/chat/prompts                               shared and your own prompts
  PUT    /api/chat/prompts/{id}                          {content, expected_revision}: your own prompt
  POST   /api/chat/prompts/{id}/render                   {scope, values} -> text for the composer (not sent)
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request

from ..control import authz
from . import engine_routes as er
from .authoring import AuthoringService, service_for


def mount(app: FastAPI, ctx) -> AuthoringService:
    engine = ctx.engine
    svc = service_for(ctx.hub_dir, engine.engine, ctx.hub)
    ctx.authoring = svc
    call = er.call
    A = "/api/chat/authoring"

    async def actor_of(request: Request, mutation: bool = False):
        user = await (ctx.mutation(request) if mutation else ctx.user(request))
        return user, await asyncio.to_thread(engine.actor, user)

    async def need(user, capability: str) -> None:
        p = await asyncio.to_thread(authz.principal_for, user, ctx.hub_dir)
        if not p.can(capability, ctx.hub):
            raise authz.denied(capability)

    def writable() -> None:
        """A hosted hub's files come from its image (HUBZOID_DATA_DIR): an edit
        here would be lost on the next deploy, so it is made in Git instead."""
        from fastapi import HTTPException

        from ..datadir import hub_read_only

        if hub_read_only(ctx.hub_dir):
            raise HTTPException(409, detail={
                "code": "hub_read_only",
                "message": "This agent's files come from its Git repository. Change them there and redeploy."})

    @app.get(f"{A}/files")
    async def files(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        from ..datadir import hub_read_only

        return {"items": await call(svc.files), "hub": ctx.hub, "read_only": hub_read_only(ctx.hub_dir)}

    @app.get(f"{A}/files/content")
    async def content(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        path = request.query_params.get("path") or ""
        text, digest = await call(svc.read, path)
        if text is None:
            from fastapi import HTTPException

            raise HTTPException(404, detail={"code": "not_found", "message": f"{path} doesn't exist."})
        return {"path": path, "text": text, "sha256": digest}

    @app.get(f"{A}/files/versions")
    async def versions(request: Request):
        user, _ = await actor_of(request)
        await need(user, "authoring.draft")
        return {"items": await call(svc.versions, request.query_params.get("path") or "")}

    @app.post(f"{A}/openapi/preview")
    async def openapi(request: Request):
        user, _ = await actor_of(request, True)
        await need(user, "authoring.draft")
        body = await er.read_json(request, er.MESSAGE_BODY_LIMIT)
        return await call(svc.openapi_preview, body.get("spec"), body.get("selected"))

    @app.get("/api/chat/prompts")
    async def prompts(request: Request):
        _, actor = await actor_of(request)
        return {"items": await call(svc.prompts, actor)}

    @app.put("/api/chat/prompts/{pid}")
    async def put_prompt(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.put_personal_prompt, actor, pid, str(body.get("content") or ""),
                          body.get("expected_revision"))

    @app.post("/api/chat/prompts/{pid}/render")
    async def render(pid: str, request: Request):
        _, actor = await actor_of(request, True)
        body = await er.read_json(request)
        return await call(svc.render, actor, pid, str(body.get("scope") or "hub"), body.get("values") or {})

    return svc


async def _reload_runtime(ctx) -> bool:
    """New chat replies use the published instructions and skills at once: the
    chat's runtime is rebuilt from the hub's files. Replies already running keep
    the runtime they started with. (Other surfaces of this bridge, such as
    Slack and MCP, pick the change up when the hub restarts.)

    The rebuild runs in the bridge's lifespan task (``runtime_reloader``), so a
    runtime is opened and closed in one task and the one it replaces is closed
    once no reply is running. Without that loop (an app without its lifespan)
    nothing is rebuilt; the next start applies the change."""
    request = getattr(ctx, "reload_runtime", None)
    if request is None:
        return False
    return await request()


async def runtime_reloader(ctx, hub_dir, original, busy, *, idle_check: float = 2.0,
                           max_wait: float = 900.0) -> None:
    """The lifespan task behind ``_reload_runtime``. ``original`` is the
    runtime the bridge started with: other surfaces use it, so only runtimes
    this loop opened are ever closed here (when replaced and nothing is
    running, or after ``max_wait``; all of them when the bridge stops)."""
    import logging
    import time

    from .. import runtime as runtime_lib

    log = logging.getLogger("hubzoid.chat.authoring")
    queue: asyncio.Queue = asyncio.Queue()
    retiring: list[tuple[object, float]] = []
    current = None

    async def request() -> bool:
        done = asyncio.get_running_loop().create_future()
        await queue.put(done)
        return await done

    async def close(rt) -> None:
        closer = getattr(rt, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001
                log.warning("authoring: closing a replaced runtime failed", exc_info=True)

    ctx.reload_runtime = request
    try:
        while True:
            try:
                done = await asyncio.wait_for(queue.get(), idle_check)
            except asyncio.TimeoutError:
                done = None
            if retiring and (not busy() or any(time.monotonic() - t > max_wait for _, t in retiring)):
                for rt, _ in retiring:
                    await close(rt)
                retiring.clear()
            if done is None:
                continue
            try:
                new = await asyncio.to_thread(runtime_lib.build, hub_dir)
                opener = getattr(new, "aopen", None)
                if opener is not None:
                    await opener()
            except Exception:  # noqa: BLE001 - the file is published; the next restart applies it
                log.warning("authoring: runtime rebuild failed", exc_info=True)
                if not done.done():
                    done.set_result(False)
                continue
            ctx.runtime = new
            if getattr(ctx, "models", None) is not None:
                ctx.models.runtime = new
            if current is not None and current is not original:
                retiring.append((current, time.monotonic()))
            current = new
            if not done.done():
                done.set_result(True)
    finally:
        ctx.reload_runtime = None
        for rt, _ in retiring:
            await close(rt)
        if current is not None and current is not original:
            await close(current)
