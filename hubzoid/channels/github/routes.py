"""GitHub routes on the hub's channels router (`/api/hubs/{hub_id}/channels/{cid}/github/...`).

The register, callback and installed pages are where GitHub sends the
browser during App setup. They need no Hubzoid sign-in: each one is bound to
the one-time setup state, which only the person who started the setup has.
Everything else needs channels.manage (or channels.inspect to read).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...control import authz
from .. import config
from ..errors import ChannelError


def mount(router, hub_dir: Path) -> None:
    hub_dir = Path(hub_dir)

    async def person(request: Request, capability: str, *, mutation: bool = False):
        return await authz.require_request(request, hub_dir, capability, config.hub_of(hub_dir), mutation=mutation)

    async def github_conn(cid: str):
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
        except ChannelError as exc:
            raise exc.http() from None
        if conn.provider != "github":
            raise ChannelError("not_found", "This channel is not a GitHub connection.", status=404).http()
        return conn

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ChannelError as exc:
            raise exc.http() from None

    async def body(request: Request) -> dict:
        import json

        raw = await request.body()
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise ChannelError("invalid_request", "Send JSON.", status=400).http() from None
        return data if isinstance(data, dict) else {}

    def page(title: str, message: str, status: int = 400) -> HTMLResponse:
        import html

        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                            f"<body style='font:16px system-ui;max-width:640px;margin:48px auto'>"
                            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>", status_code=status)

    @router.post("/channels/{cid}/github/setup", status_code=201)
    async def setup_start(hub_id: str, cid: str, request: Request):
        from . import setup

        p = await person(request, "channels.manage", mutation=True)
        b = await body(request)
        conn = await github_conn(cid)
        return await run(setup.start, conn, owner=str(b.get("owner") or ""), owner_type=str(b.get("type") or ""),
                         name=str(b.get("name") or conn.id), actor=p.email)

    @router.get("/channels/{cid}/github/setup")
    async def setup_status(hub_id: str, cid: str, request: Request):
        from . import setup

        await person(request, "channels.inspect")
        return setup.status(await github_conn(cid))

    @router.delete("/channels/{cid}/github/setup", status_code=204)
    async def setup_cancel(hub_id: str, cid: str, request: Request):
        from fastapi.responses import Response

        from . import setup

        p = await person(request, "channels.manage", mutation=True)
        await run(setup.cancel, await github_conn(cid), p.email)
        return Response(status_code=204)

    @router.get("/channels/{cid}/github/register", include_in_schema=False)
    async def register(hub_id: str, cid: str, setup: str = "", state: str = ""):
        from . import setup as s

        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
            return HTMLResponse(await asyncio.to_thread(s.registration_page, conn, setup, state))
        except ChannelError as exc:
            return page("GitHub setup", exc.message, exc.status)

    @router.get("/channels/{cid}/github/callback", include_in_schema=False)
    async def callback(hub_id: str, cid: str, code: str = "", state: str = ""):
        from . import setup as s

        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
            nxt = await asyncio.to_thread(s.callback, conn, code, state)
        except ChannelError as exc:
            return page("GitHub setup", exc.message, exc.status)
        return RedirectResponse(nxt, status_code=303)

    @router.get("/channels/{cid}/github/installed", include_in_schema=False)
    async def installed(hub_id: str, cid: str, installation_id: int = 0, state: str = "", setup_action: str = ""):
        from . import setup as s

        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
            # The public return from GitHub must carry this setup's one-time state.
            # Without it, a signed-in admin verifies the installation in the Console.
            await asyncio.to_thread(s.installed, conn, installation_id, state)
        except ChannelError as exc:
            return page("GitHub setup", exc.message, exc.status)
        public = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
        return RedirectResponse(f"{public}/portal/#/agents/{config.hub_of(hub_dir)}/channels/{cid}", status_code=303)

    @router.post("/channels/{cid}/github/setup/verify")
    async def setup_verify(hub_id: str, cid: str, request: Request):
        from . import setup

        p = await person(request, "channels.manage", mutation=True)
        b = await body(request)
        return await run(setup.installed, await github_conn(cid), int(b.get("installation_id") or 0), None,
                         actor=p.email)

    @router.post("/channels/{cid}/github/import-app")
    async def import_app(hub_id: str, cid: str, request: Request):
        from . import setup

        p = await person(request, "channels.manage", mutation=True)
        b = await body(request)
        return await run(setup.import_app, await github_conn(cid), app_id=int(b.get("app_id") or 0),
                         installation_id=int(b.get("installation_id") or 0), owner_id=int(b.get("owner_id") or 0),
                         actor=p.email)

    @router.get("/channels/{cid}/github/repositories")
    async def repositories(hub_id: str, cid: str, request: Request):
        from . import setup

        await person(request, "channels.inspect")
        return {"items": await run(setup.repositories, await github_conn(cid))}

    @router.post("/channels/{cid}/github/repositories/resolve")
    async def resolve(hub_id: str, cid: str, request: Request):
        from . import setup

        await person(request, "channels.manage", mutation=True)
        b = await body(request)
        conn = await github_conn(cid)
        return {"repositories": await run(setup.resolve_names, conn, list(b.get("names") or [])),
                "revision": conn.revision}

    @router.post("/channels/{cid}/github/configure")
    async def configure(hub_id: str, cid: str, request: Request):
        from . import setup

        p = await person(request, "channels.manage", mutation=True)
        b = await body(request)
        return await run(setup.configure, await github_conn(cid), expected_revision=int(b.get("expected_revision") or 0),
                         actor=p.email, repositories_=b.get("repositories"), sets=b.get("set") or {},
                         any_secret=p.org_admin)

    @router.post("/channels/{cid}/github/intake", status_code=202)
    async def intake(hub_id: str, cid: str, request: Request):
        from .. import router as channel_router
        from . import setup

        await person(request, "channels.manage", mutation=True)
        b = await body(request)
        out = await run(setup.manual_intake, await github_conn(cid), url=str(b.get("url") or ""),
                        task=str(b.get("task") or ""), key=request.headers.get("idempotency-key"))
        channel_router.kick(hub_dir)
        return JSONResponse(out, status_code=202)

    @router.post("/channels/{cid}/github/operations")
    async def operations(hub_id: str, cid: str, request: Request):
        from . import operations as ops

        await person(request, "channels.manage", mutation=True)
        b = await body(request)
        return await run(ops.call, await github_conn(cid), str(b.get("name") or ""), b.get("arguments") or {})
