"""Intake review routes (`/api/hubs/{hub_id}/intake/...`).

Reading needs `intake.read`; release, reject and retry need `intake.manage`.
Releasing admits one message to start its work. It approves nothing that
work later wants to send."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .. import hub_routes
from ..channels import config as config_lib
from ..channels.errors import ChannelError
from ..control import authz
from . import service

_NO_STORE = {"Cache-Control": "no-store"}


def mount(app, hub_dir: Path, **_ctx) -> None:
    hub_dir = Path(hub_dir)
    router = hub_routes.router(hub_dir, tags=["intake"])

    async def person(request: Request, capability: str, *, mutation: bool = False):
        return await authz.require_request(request, hub_dir, capability, config_lib.hub_of(hub_dir),
                                           mutation=mutation)

    async def run(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ChannelError as exc:
            raise exc.http() from None

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            raise ChannelError("validation_failed", "Send a JSON object.", status=422).http() from None
        if not isinstance(data, dict):
            raise ChannelError("validation_failed", "Send a JSON object.", status=422).http()
        return data

    @router.get("/intake")
    async def list_items(hub_id: str, request: Request, channel: str | None = None, state: str | None = None,
                         cursor: str | None = None, limit: int = 50):
        await person(request, "intake.read")
        out = await run(service.list_items, hub_dir, channel=channel, state=state, cursor=cursor, limit=limit)
        return JSONResponse(out, headers=_NO_STORE)

    @router.get("/intake/{item_id}")
    async def show(hub_id: str, item_id: str, request: Request):
        await person(request, "intake.read")
        item = await run(service.require, hub_dir, item_id)
        return JSONResponse(service.view(item, full=True, hub_dir=hub_dir), headers=_NO_STORE)

    @router.get("/intake/{item_id}/attachments/{attachment_id}")
    async def attachment(hub_id: str, item_id: str, attachment_id: str, request: Request):
        await person(request, "intake.read")
        info, data = await run(service.attachment_bytes, hub_dir, attachment_id)
        safe = "".join(ch for ch in info["filename"] if ch.isalnum() or ch in "._- ")[:100] or "attachment"
        # Always a download, never rendered: the bytes came from outside.
        return Response(data, media_type="application/octet-stream", headers={
            **_NO_STORE, "Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff"})

    async def decide(fn, item_id: str, request: Request, **extra):
        p = await person(request, "intake.manage", mutation=True)
        b = await body(request)
        try:
            revision = int(b.get("expected_revision"))
        except (TypeError, ValueError):
            raise ChannelError("validation_failed", "expected_revision is required.", status=422).http() from None
        kwargs = {"actor": p.email, "expected_revision": revision, **extra}
        if fn is not service.retry:
            kwargs["reason"] = str(b.get("reason") or "")
        out = await run(fn, hub_dir, item_id, **kwargs)
        return JSONResponse(service.view(out), headers=_NO_STORE)

    @router.post("/intake/{item_id}/release")
    async def release(hub_id: str, item_id: str, request: Request):
        return await decide(service.release, item_id, request)

    @router.post("/intake/{item_id}/reject")
    async def reject(hub_id: str, item_id: str, request: Request):
        return await decide(service.reject, item_id, request)

    @router.post("/intake/{item_id}/retry")
    async def retry(hub_id: str, item_id: str, request: Request):
        return await decide(service.retry, item_id, request)

    app.include_router(router)
