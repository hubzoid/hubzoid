"""Email routes on the hub's channels router
(`/api/hubs/{hub_id}/channels/{cid}/email/...`).

The consent callback needs no Hubzoid sign-in: it is bound to the one-time
state of the attempt an administrator started, and refused when used twice
or late."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from ...control import authz
from .. import config
from ..errors import ChannelError


def mount(router, hub_dir: Path) -> None:
    hub_dir = Path(hub_dir)

    async def person(request: Request, capability: str, *, mutation: bool = False):
        return await authz.require_request(request, hub_dir, capability, config.hub_of(hub_dir), mutation=mutation)

    async def email_conn(cid: str):
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
        except ChannelError as exc:
            raise exc.http() from None
        if conn.provider != "email":
            raise ChannelError("not_found", "This channel is not an email connection.", status=404).http()
        return conn

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ChannelError as exc:
            raise exc.http() from None

    @router.get("/channels/{cid}/email/status")
    async def status(hub_id: str, cid: str, request: Request):
        from . import status as st

        await person(request, "channels.inspect")
        return await run(st.status, await email_conn(cid))

    @router.post("/channels/{cid}/email/connect", status_code=201)
    async def connect(hub_id: str, cid: str, request: Request):
        from . import oauth

        p = await person(request, "channels.manage", mutation=True)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        return await run(oauth.connect, await email_conn(cid), actor=p.email, mode=(body or {}).get("mode") or "approve")

    @router.get("/channels/{cid}/email/oauth/callback", include_in_schema=False)
    async def callback(hub_id: str, cid: str, code: str = "", state: str = "", error: str = ""):
        import html

        from . import oauth

        def page(title, message, code_=200):
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><body "
                                f"style='font:16px system-ui;max-width:640px;margin:48px auto'><h1>{html.escape(title)}"
                                f"</h1><p>{html.escape(message)}</p>", status_code=code_)

        if error:
            return page("Mailbox not connected", f"The provider said: {error}. Nothing was saved.", 400)
        try:
            conn = await asyncio.to_thread(config.active, hub_dir, cid)
            out = await asyncio.to_thread(oauth.callback, conn, code=code, state=state)
        except ChannelError as exc:
            return page("Mailbox not connected", exc.message, exc.status)
        return page("Mailbox connected", f"Hubzoid can now read new mail in {out['mailbox']}. You can close this "
                                         "page and run hubzoid channels doctor.")

    @router.post("/channels/{cid}/email/poll")
    async def poll(hub_id: str, cid: str, request: Request):
        from .poller import poll_once

        await person(request, "channels.manage", mutation=True)
        return await run(poll_once, await email_conn(cid), owner="console")

    @router.post("/channels/{cid}/email/rehearse-draft")
    async def rehearse(hub_id: str, cid: str, request: Request):
        from . import status as st

        p = await person(request, "channels.manage", mutation=True)
        return await run(st.rehearse_draft, await email_conn(cid), actor=p.email)
