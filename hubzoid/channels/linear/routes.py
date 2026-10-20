"""Linear routes on the hub's channels router (`/api/hubs/{hub_id}/channels/{cid}/linear/...`).

The OAuth callback needs no Hubzoid sign-in: it is bound to the one-time
state of the attempt the administrator started, and is refused when used
twice or late."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from ...control import authz
from .. import config, store
from ..errors import ChannelError


def mount(router, hub_dir: Path) -> None:
    hub_dir = Path(hub_dir)

    async def person(request: Request, capability: str, *, mutation: bool = False):
        return await authz.require_request(request, hub_dir, capability, config.hub_of(hub_dir), mutation=mutation)

    async def linear_conn(cid: str):
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
        except ChannelError as exc:
            raise exc.http() from None
        if conn.provider != "linear":
            raise ChannelError("not_found", "This channel is not a Linear connection.", status=404).http()
        return conn

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ChannelError as exc:
            raise exc.http() from None

    @router.get("/channels/{cid}/linear/setup")
    async def setup(hub_id: str, cid: str, request: Request):
        from . import oauth

        await person(request, "channels.inspect")
        return await run(oauth.setup_checklist, await linear_conn(cid))

    @router.post("/channels/{cid}/linear/connect", status_code=201)
    async def connect(hub_id: str, cid: str, request: Request):
        from . import oauth

        p = await person(request, "channels.manage", mutation=True)
        return await run(oauth.connect, await linear_conn(cid), actor=p.email)

    @router.get("/channels/{cid}/linear/oauth/callback", include_in_schema=False)
    async def callback(hub_id: str, cid: str, code: str = "", state: str = "", error: str = ""):
        import html

        from . import oauth

        def page(title, message, status=200):
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><body "
                                f"style='font:16px system-ui;max-width:640px;margin:48px auto'><h1>{html.escape(title)}"
                                f"</h1><p>{html.escape(message)}</p>", status_code=status)

        if error:
            return page("Linear not connected", f"Linear said: {error}. Nothing was saved.", 400)
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
            out = await asyncio.to_thread(oauth.callback, conn, code=code, state=state)
        except ChannelError as exc:
            return page("Linear not connected", exc.message, exc.status)
        return page("Linear connected", f"The app is installed in {out['organization']}. You can close this page "
                                        "and run hubzoid channels doctor.")

    @router.post("/channels/{cid}/linear/disconnect", status_code=202)
    async def disconnect(hub_id: str, cid: str, request: Request):
        from . import oauth

        p = await person(request, "channels.manage", mutation=True)
        return await run(oauth.disconnect, await linear_conn(cid), actor=p.email)

    @router.get("/channels/{cid}/linear/sessions")
    async def sessions(hub_id: str, cid: str, request: Request, cursor: str | None = None, limit: int = 50):
        await person(request, "channels.inspect")
        conn = await linear_conn(cid)
        out = await run(store.list_threads, hub_dir, conn.hub, cid, limit=limit, cursor=cursor)
        return {"items": [{"thread_id": t["id"], "agent_session_id": t["origin_json"]["coordinates"].get(
            "agent_session_id"), "url": t["origin_json"].get("url"), "requester": t["requester"],
            "updated": t["updated"]} for t in out["items"]], "next_cursor": out["next_cursor"]}
