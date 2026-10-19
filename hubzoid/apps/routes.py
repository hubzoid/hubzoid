"""HTTP routes for cards, in the Console's API (``/portal/api/...``).

* ``GET /hubs/{hub}/apps/config``, ``POST .../apps/validate`` and
  ``PUT .../apps/config`` (``expected_revision``): the hub's managers.
* ``POST .../apps/invoke``: run a card's tool as the caller (its own guards
  decide), with an ``Idempotency-Key``.
* ``GET .../apps/instances[/{id}]``, ``POST .../apps/instances/{id}/dispatch``,
  ``.../revoke``: the instance's requester only (anyone else gets 404).
* ``POST .../apps/host``: the same dispatch for assistant-ui's remote host,
  which names the instance as its routing key (``params.serverId``).
* ``POST .../apps/probe``: how a client would get each card.
* ``GET /apps/sandbox`` and ``/apps/sandbox/frame.html``: the card frame's
  separate origin, and the page served there.

Errors are ``{"detail": {"code", "message", ...}}``.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..work_items import store as wi_store
from ..work_items.errors import WorkItemError
from ..work_items.routes import _call, _raise, _writer
from . import cards



class ApplyBody(BaseModel):
    expected_revision: int | None = None
    config: dict | None = None


class InvokeBody(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


class DispatchBody(BaseModel):
    method: str = Field(min_length=1, max_length=64)
    params: dict = Field(default_factory=dict)


class RevokeBody(BaseModel):
    reason: str = Field(default="revoked", max_length=500)


def _identity(hub_dir: Path, subject: str):
    """The local operator's identity, for the CLI."""
    from ..chat.runs import web_identity

    return web_identity(hub_dir, subject)


def _request_identity(hub_dir: Path, request: Request, run_id: str):
    """The signed-in person as an admitted run (``access.run_identity``), with
    the session or key that made this request: every later check sees it, so
    a sign-out or a revoked key stops a card's call too."""
    from .. import access, auth
    from ..access import run_identity

    user = auth.require_user(request, hub_dir)
    try:
        record = run_identity.admit_or_refuse(
            hub_dir, run_kind="chat", run_id=run_id, acts_as_id=user.id, acts_as_email=user.email,
            started_from="chat", person_present=user.credential != "api_key",
            credential_kind=user.credential, credential_id=user.credential_id)
    except run_identity.NotAdmitted as exc:
        raise WorkItemError("unavailable", str(exc), status=503, retryable=True) from None
    groups = access.effective_groups(hub_dir, email=user.email, surface="web", header_groups=None)
    return run_identity.identity(record, "web", groups)


def register(router: APIRouter, require_person: Callable, hub_path: Callable[[Any, str], Path]) -> None:
    from ..work_items import service as wi_service
    from .. import mcp_server

    writer = _writer(require_person)

    def hub_for(actor, hub: str, *, manage: bool = False) -> Path:
        try:
            hub_dir = hub_path(actor, hub)
        except KeyError:
            raise HTTPException(404, detail={"code": "hub_not_found", "message": "There is no such hub here."})
        if manage and not wi_service._can(hub_dir, actor.subject, "manage_access"):
            raise HTTPException(403, detail={"code": "permission_denied",
                                             "message": "Card settings are for the hub's administrators."})
        return hub_dir

    @router.get("/hubs/{hub}/apps/config")
    async def get_config(hub: str, actor=Depends(require_person)):
        return await _call(cards.get, hub_for(actor, hub, manage=True))

    @router.post("/hubs/{hub}/apps/validate")
    async def validate(hub: str, actor=Depends(writer)):
        hub_dir = hub_for(actor, hub, manage=True)
        return await _call(lambda: cards.summary(hub_dir, cards.validate(hub_dir)))

    @router.put("/hubs/{hub}/apps/config")
    async def apply(hub: str, body: ApplyBody, actor=Depends(writer)):
        hub_dir = hub_for(actor, hub, manage=True)

        return await _call(cards.apply, hub_dir, expected=body.expected_revision, actor=actor.subject,
                           apps_doc=body.config)

    @router.post("/hubs/{hub}/apps/invoke")
    async def invoke(hub: str, request: Request, response: Response, body: InvokeBody, actor=Depends(writer)):
        hub_dir = hub_for(actor, hub)
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise HTTPException(400, detail={"code": "invalid_request", "message": "Send an Idempotency-Key."})
        ident = await _call(_request_identity, hub_dir, request, f"card-invoke:{uuid.uuid4().hex}")
        status, payload = await _call(wi_store.idempotent, hub_dir, actor.subject, f"{hub}:apps.invoke", key,
                                      body.model_dump(),
                                      lambda: (200, asyncio.run(_invoke(hub_dir, actor.subject, body, ident))))
        response.status_code = status
        return payload

    @router.get("/hubs/{hub}/apps/instances")
    async def instances(hub: str, limit: int = 50, actor=Depends(require_person)):
        hub_dir = hub_for(actor, hub)
        from . import store

        return {"instances": await _call(store.list_instances, hub_dir, cards.hub_key(hub_dir),
                                         requester=actor.subject, limit=limit)}

    @router.get("/hubs/{hub}/apps/instances/{instance_id}")
    async def instance(hub: str, instance_id: str, actor=Depends(require_person)):
        return await _call(cards.view, hub_for(actor, hub), instance_id, actor.subject)

    async def _dispatch(hub_dir: Path, instance_id: str, actor, body: DispatchBody, request: Request):
        ident = await _call(_request_identity, hub_dir, request, f"card:{instance_id[:36]}:{uuid.uuid4().hex}")

        async def call_tool(name, args, identity, classes):
            return await mcp_server.call_registry_tool(hub_dir, name, args, identity, classes)

        try:
            return await cards.dispatch(hub_dir, instance_id, actor.subject, body.method, body.params,
                                        identity=ident, call_tool=call_tool)
        except WorkItemError as exc:
            _raise(exc)

    @router.post("/hubs/{hub}/apps/instances/{instance_id}/dispatch")
    async def dispatch(hub: str, instance_id: str, body: DispatchBody, request: Request, actor=Depends(writer)):
        return await _dispatch(hub_for(actor, hub), instance_id, actor, body, request)

    @router.post("/hubs/{hub}/apps/host")
    async def host(hub: str, body: DispatchBody, request: Request, actor=Depends(writer)):
        params = dict(body.params)
        instance_id = params.pop("serverId", None)
        if not isinstance(instance_id, str) or not instance_id:
            raise HTTPException(400, detail={"code": "invalid_request", "message": "Name the card (serverId)."})
        return await _dispatch(hub_for(actor, hub), instance_id, actor, DispatchBody(method=body.method,
                                                                                     params=params), request)

    @router.post("/hubs/{hub}/apps/instances/{instance_id}/revoke")
    async def revoke(hub: str, instance_id: str, body: RevokeBody = Body(default_factory=RevokeBody),
                     actor=Depends(writer)):
        return await _call(cards.revoke, hub_for(actor, hub), instance_id, actor.subject, body.reason)

    @router.post("/hubs/{hub}/apps/probe")
    async def probe(hub: str, body: dict = Body(default_factory=dict), actor=Depends(require_person)):
        return await _call(cards.probe, hub_for(actor, hub), body.get("client_capabilities"))

    @router.get("/apps/sandbox")
    async def sandbox(parent: str, actor=Depends(require_person)):
        origin = sandbox_origin(parent)
        if origin is None:
            raise HTTPException(409, detail={
                "code": "sandbox_origin_missing",
                "message": "Cards need a separate origin here. Set HUBZOID_APPS_SANDBOX_ORIGIN, or use "
                           "localhost or 127.0.0.1."})
        return {"origin": origin, "frame": f"{origin}/portal/api/apps/sandbox/frame.html"}

    @router.get("/apps/sandbox/frame.html")
    async def frame(request: Request, origin: str):
        """The page a card frame loads, on the sandbox origin. No sign-in (no
        cookie reaches this origin), and nothing to read: it only relays."""
        from .. import appmode

        parent = appmode.normalize_origin(origin)
        host = (request.headers.get("host") or "").lower()
        expected = sandbox_origin(parent) if parent else None
        # Served only on the sandbox origin the chat at `parent` uses.
        if expected is None or urlparse(expected).netloc.lower() != host:
            raise HTTPException(403, detail={"code": "forbidden", "message": "Not a card sandbox request."})
        return HTMLResponse(_frame_html(parent), headers=_frame_headers(parent))


def sandbox_origin(parent: str) -> str | None:
    """The card frame's origin for a page at ``parent``: the configured one,
    or on loopback the other of ``localhost`` and ``127.0.0.1`` on the same
    port (the browser keeps their cookies apart). None when neither applies."""
    from .. import appmode

    parent = appmode.normalize_origin(parent)
    if not parent:
        return None
    configured = appmode.normalize_origin(os.environ.get("HUBZOID_APPS_SANDBOX_ORIGIN", ""))
    p = urlparse(parent)
    if configured:
        # Only for the deployment's own chat origins.
        return configured if parent in set(appmode.allowed_origins()) and configured != parent else None
    if p.hostname not in ("localhost", "127.0.0.1"):
        return None
    other = "127.0.0.1" if p.hostname == "localhost" else "localhost"
    return f"{p.scheme}://{other}" + (f":{p.port}" if p.port else "")


def _frame_headers(parent: str) -> dict:
    csp = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
           "font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
           f"frame-ancestors {parent}")
    return {"Content-Security-Policy": csp, "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "cross-origin"}


def _frame_html(parent: str) -> str:
    page = (Path(__file__).parent / "sandbox_frame.html").read_text(encoding="utf-8")
    return page.replace('"__HZ_PARENT_ORIGIN__"', json.dumps(parent))


async def _invoke(hub_dir: Path, subject: str, body: InvokeBody, identity=None) -> dict:
    """Run a card's tool as ``subject`` (``identity``, the admitted run of the
    request; the CLI passes none and runs as the local operator) and return
    the instance it made."""
    from .. import mcp_server

    _row, res = await asyncio.to_thread(cards.active, hub_dir, fresh=True)
    card = res.cards.get(body.card_id) if res and res.enabled else None
    if card is None:
        raise WorkItemError("not_found", "No such active card here.", status=404)
    ident = identity if identity is not None else await asyncio.to_thread(_identity, hub_dir, subject)
    out = await mcp_server.call_registry_tool(hub_dir, card.tool, body.arguments, ident, None)
    if out.get("refused"):
        raise WorkItemError("tool_unknown", out["message"], status=404)
    env = out["card"]
    if env is None:
        return {"instance": None, "text": out["text"], "card": False}
    return {"instance": await asyncio.to_thread(cards.view, hub_dir, env["instance_id"], subject),
            "text": env["text"], "card": True}
